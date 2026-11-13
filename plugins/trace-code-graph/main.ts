import { FileSystemAdapter, ItemView, Notice, Plugin, WorkspaceLeaf } from "obsidian";
import { lstat, open, realpath } from "node:fs/promises";
import { isAbsolute, join, relative, resolve, sep } from "node:path";

const VIEW = "trace-code-graph";
const DATA = ".trace-graphs";
const PROJECT = /^[a-zA-Z0-9-]{1,45}-[a-f0-9]{16}$/;
const ID = /^(repository|[a-f0-9]{32})$/;
const GENERATION = /^[a-f0-9]{16}$/;
const PAGE = 24;
const MAX_VISIBLE = 240;
const MAX_READ = 8 * 1024 * 1024;
const MAX_SOURCE = 256_000;
const WIDTH = 216;
const HEIGHT = 56;
const DX = 270;
const DY = 74;
const SVG = "http://www.w3.org/2000/svg";
const GROUPS = new Set(["repository", "directory", "package", "module", "namespace", "class", "interface"]);
const PRIVATE_DIRS = new Set([
    ".git", ".hg", ".agent-memory", ".obsidian", ".trace-graphs", ".venv", ".pytest_cache",
    ".hypothesis", "__pycache__", "node_modules", ".astro", ".next", "dist",
]);
const PRIVATE_FILES = /^(\.npmrc|\.pypirc|credentials\.json|id_rsa|id_ed25519)$|\.(key|pem|pfx|p12|sqlite|sqlite3|db)$/i;

type Entity = {
    id: string; parent: string | null; kind: string; name: string; path: string;
    line: number | null; openable: boolean; children: number;
};
type Branch = { node: Entity; children: Entity[] };
type Link = [peer: string, kind: string, confidence: string, path: string, line: number | null, column: number | null,
    openable: boolean, peerName: string];
type Links = { incoming: Link[]; outgoing: Link[] };
type Catalog = [qualified: string, entity: Entity][];
type Evidence = { type: string; reference: string | null; freshness: string; availability: string;
    path?: string; commit?: string; message_id?: number };
type HistoryEvent = { id: number; type: string; relationship: string; status: string; freshness: string;
    supersededBy: number | null; evidence: Evidence[] };
type History = { attached: Record<string, number[]>; records: Record<number, HistoryEvent> };
type Manifest = { version: number; name: string; root: string; generation: string; nodeCount: number;
    edgeCount: number; links: string[]; indexGeneration: string; history?: boolean };
type ViewState = { project: string; root: string; selected: string; expanded: string[]; zoom: number; cross: boolean };
type Positioned = { node: Entity; parent?: string; depth: number; x: number; y: number };

function initial(project = ""): ViewState {
    return { project, root: "repository", selected: "repository", expanded: [], zoom: 1, cross: true };
}

async function readJson<T>(adapter: FileSystemAdapter, path: string, marked = false): Promise<T> {
    const root = await realpath(adapter.getBasePath());
    const local = resolve(root, path);
    if (relative(local, await realpath(local)) !== "") throw new Error("Redirected graph files cannot be opened.");
    const stat = await adapter.stat(path);
    if (stat?.type !== "file" || stat.size > MAX_READ) throw new Error("A graph file is missing or exceeds its size limit.");
    const text = await adapter.read(path);
    if (text.length > MAX_READ) throw new Error("A graph file exceeds its size limit.");
    return JSON.parse(marked ? text.slice(text.indexOf("\n") + 1) : text) as T;
}

class Snapshot {
    private cache = new Map<string, Promise<unknown>>();
    private catalogRead?: Promise<Catalog>;
    private historyRead?: Promise<History>;

    private constructor(private adapter: FileSystemAdapter, private base: string, readonly meta: Manifest) {}

    static async open(adapter: FileSystemAdapter, project: string): Promise<Snapshot> {
        if (!PROJECT.test(project)) throw new Error("Select a TRACE project.");
        const base = `${DATA}/${project}`;
        const meta = await readJson<Manifest>(adapter, `${base}/current.json`, true);
        if (meta.version !== 1 || !GENERATION.test(meta.generation) || meta.root !== "repository"
            || !Number.isInteger(meta.nodeCount) || meta.nodeCount < 1 || meta.nodeCount > 50_000
            || !Array.isArray(meta.links)) throw new Error("This graph format is unsupported. Export it again with TRACE.");
        return new Snapshot(adapter, `${base}/${meta.generation}`, meta);
    }

    private shard<T>(type: "nodes" | "links", key: string): Promise<Record<string, T>> {
        const file = `${type}-${key}.json`;
        const existing = this.cache.get(file);
        this.cache.delete(file);
        const request = existing ?? readJson<Record<string, T>>(this.adapter, `${this.base}/${file}`).catch((error) => {
            this.cache.delete(file);
            throw error;
        });
        this.cache.set(file, request);
        if (this.cache.size > 8) this.cache.delete(this.cache.keys().next().value!);
        return request as Promise<Record<string, T>>;
    }

    async branch(id: string): Promise<Branch | undefined> {
        if (!ID.test(id)) return undefined;
        const shard = await this.shard<Branch>("nodes", id[0]);
        return shard[id];
    }

    async links(id: string): Promise<Links> {
        if (ID.test(id) && this.meta.links.includes(id[0])) {
            const shard = await this.shard<Links>("links", id[0]);
            if (shard[id]) return shard[id];
        }
        return { incoming: [], outgoing: [] };
    }

    async search(query: string): Promise<Entity[]> {
        this.catalogRead ??= readJson<Catalog>(this.adapter, `${this.base}/catalog.json`).catch((error) => {
            this.catalogRead = undefined;
            throw error;
        });
        const normalized = query.toLowerCase();
        const words = normalized.split(/\s+/);
        const exact: Entity[] = [];
        const partial: Entity[] = [];
        for (const [qualified, node] of await this.catalogRead) {
            const name = node.name.toLowerCase();
            const path = node.path.toLowerCase();
            if (name === normalized || path === normalized) {
                if (exact.length < PAGE) exact.push(node);
            } else if (partial.length < PAGE && words.every((word) => `${name} ${path} ${qualified.toLowerCase()}`.includes(word))) {
                partial.push(node);
            }
        }
        return exact.concat(partial).slice(0, PAGE);
    }

    async history(id: string): Promise<HistoryEvent[]> {
        if (!this.meta.history) return [];
        this.historyRead ??= readJson<History>(this.adapter, `${this.base}/history.json`).catch((error) => {
            this.historyRead = undefined;
            throw error;
        });
        const { attached, records } = await this.historyRead;
        return (attached[id] ?? []).map((key) => records[key]).filter(Boolean);
    }
}

function control(parent: HTMLElement, label: string, action: () => void, options: { cls?: string; title?: string } = {}) {
    const button = parent.createEl("button", { text: label, cls: options.cls, attr: { type: "button" } });
    if (options.title) {
        button.title = options.title;
        button.setAttribute("aria-label", options.title);
    }
    button.addEventListener("click", action);
    return button;
}

function entityLabel(node: Entity, project: string): string {
    return node.id === "repository" ? project : node.name;
}

async function sourcePath(root: string, path: string): Promise<string> {
    const parts = path.split("/");
    if (path.length > 1024 || /[\\:\u0000-\u001f\u007f-\u009f]/.test(path)
        || parts.some((part) => !part || part === "." || part === ".." || /[. ]$/.test(part)
            || part.toLowerCase().startsWith(".env") || PRIVATE_DIRS.has(part.toLowerCase()))
        || PRIVATE_FILES.test(parts[parts.length - 1])) throw new Error("This source path cannot be opened.");
    const base = await realpath(root);
    if (relative(root, base) !== "") throw new Error("The connected source folder has moved or been redirected.");
    let pathOnDisk = base;
    for (const part of parts) {
        pathOnDisk = join(pathOnDisk, part);
        if ((await lstat(pathOnDisk)).isSymbolicLink()) throw new Error("Redirected source files cannot be opened.");
    }
    const local = await realpath(pathOnDisk);
    const rest = relative(base, local);
    if (!rest || rest === ".." || rest.startsWith(`..${sep}`) || isAbsolute(rest) || relative(pathOnDisk, local) !== "") {
        throw new Error("The source file is outside the connected folder or has been redirected.");
    }
    return local;
}

class TraceGraphPlugin extends Plugin {
    private roots: Record<string, string> = {};

    async onload() {
        this.roots = (await this.loadData())?.roots ?? {};
        this.registerView(VIEW, (leaf) => new GraphView(leaf, this));
        this.addCommand({ id: "open", name: "Open TRACE Code Graph", callback: () => { void this.activate(); } });
        this.addRibbonIcon("git-fork", "Open TRACE Code Graph", () => { void this.activate(); });
    }

    private async activate() {
        try {
            const leaf = this.app.workspace.getLeavesOfType(VIEW)[0] ?? this.app.workspace.getLeaf("tab");
            if (leaf.getViewState().type !== VIEW) await leaf.setViewState({ type: VIEW, active: true });
            await this.app.workspace.revealLeaf(leaf);
        } catch {
            new Notice("TRACE Code Graph could not open.");
        }
    }

    sourceRoot(project: string) {
        return typeof this.roots[project] === "string" ? this.roots[project] : "";
    }

    async connectSource(project: string, input: string) {
        const root = input.trim();
        if (!PROJECT.test(project) || !isAbsolute(root) || root.startsWith("\\\\") || root.startsWith("//")) {
            throw new Error("Enter an absolute path on this computer.");
        }
        const local = await realpath(root);
        if (!(await lstat(local)).isDirectory() || local === resolve(local, "..")) throw new Error("Choose a repository folder.");
        const roots = { ...this.roots, [project]: local };
        await this.saveData({ roots });
        this.roots = roots;
    }

    async readSource(project: string, node: Entity) {
        const root = this.sourceRoot(project);
        if (!root || !node.openable) throw new Error("Source is unavailable.");
        const local = await sourcePath(root, node.path);
        const before = await lstat(local);
        if (!before.isFile() || before.size > MAX_SOURCE) throw new Error("Source preview needs a regular text file up to 256 KB.");
        const handle = await open(local, "r");
        try {
            const info = await handle.stat();
            const checked = await lstat(await sourcePath(root, node.path));
            if (!info.isFile() || info.ino !== checked.ino || info.dev !== checked.dev || info.size > MAX_SOURCE) {
                throw new Error("Source preview needs a regular text file up to 256 KB.");
            }
            const bytes = Buffer.alloc(MAX_SOURCE + 1);
            const { bytesRead } = await handle.read(bytes, 0, bytes.length, 0);
            if (bytesRead > MAX_SOURCE || bytes.subarray(0, bytesRead).includes(0)) throw new Error("This file cannot be previewed as text.");
            return new TextDecoder("utf-8", { fatal: true }).decode(bytes.subarray(0, bytesRead));
        } finally {
            await handle.close();
        }
    }
}

class GraphView extends ItemView {
    private state = initial();
    private data?: Snapshot;
    private adapter?: FileSystemAdapter;
    private epoch = 0;
    private loadEpoch = 0;
    private searchEpoch = 0;
    private pages = new Map<string, number>();
    private linkPage = 0;
    private project!: HTMLSelectElement;
    private results!: HTMLElement;
    private message!: HTMLElement;
    private counts!: HTMLElement;
    private breadcrumbs!: HTMLElement;
    private viewport!: HTMLElement;
    private canvas!: HTMLElement;
    private inspector!: HTMLElement;
    private cross!: HTMLInputElement;
    private zoomLabel!: HTMLElement;

    constructor(leaf: WorkspaceLeaf, private plugin: TraceGraphPlugin) { super(leaf); }

    getViewType() { return VIEW; }
    getDisplayText() { return "TRACE Code Graph"; }
    getIcon() { return "git-fork"; }
    getState() { return { ...this.state, expanded: this.state.expanded.slice(-MAX_VISIBLE) }; }

    async setState(state: Partial<ViewState>, result: Parameters<ItemView["setState"]>[1]) {
        this.state = {
            ...initial(), ...state,
            expanded: Array.isArray(state.expanded) ? state.expanded.filter((id) => ID.test(id)).slice(-MAX_VISIBLE) : [],
            zoom: Number.isFinite(state.zoom) ? Math.max(0.4, Math.min(1.6, state.zoom!)) : 1,
        };
        await super.setState(state, result);
        if (this.adapter && this.project) await this.loadGraph();
    }

    async onOpen() {
        const root = this.contentEl;
        root.empty();
        root.addClass("trace-graph");
        if (!(this.app.vault.adapter instanceof FileSystemAdapter)) {
            root.setText("TRACE Code Graph needs a local desktop vault.");
            return;
        }
        this.adapter = this.app.vault.adapter;
        const toolbar = root.createDiv({ cls: "trace-toolbar" });
        this.project = toolbar.createEl("select", { attr: { "aria-label": "TRACE project" } });
        this.project.addEventListener("change", () => {
            this.state = initial(this.project.value);
            this.pages.clear();
            this.linkPage = 0;
            this.save();
            void this.loadGraph();
        });
        const search = toolbar.createEl("form", { cls: "trace-search" });
        const input = search.createEl("input", { type: "search", placeholder: "Find a symbol or path",
            attr: { "aria-label": "Find a symbol or path", maxlength: "240" } });
        search.createEl("button", { text: "Find", attr: { type: "submit" } });
        search.addEventListener("submit", (event) => { event.preventDefault(); void this.search(input.value); });
        control(toolbar, "Refresh", () => { void this.loadGraph(); }, { title: "Refresh graph snapshot" });
        const label = toolbar.createEl("label", { cls: "trace-toggle" });
        this.cross = label.createEl("input", { type: "checkbox" });
        label.appendText("Cross-links");
        this.cross.addEventListener("change", () => { this.state.cross = this.cross.checked; this.changed(); });
        control(toolbar, "−", () => this.zoom(-0.15), { title: "Zoom out" });
        this.zoomLabel = control(toolbar, "100%", () => this.zoom(0), { title: "Reset zoom" });
        control(toolbar, "+", () => this.zoom(0.15), { title: "Zoom in" });
        this.results = root.createDiv({ cls: "trace-results", attr: { hidden: "" } });
        const navigation = root.createDiv({ cls: "trace-navigation" });
        this.breadcrumbs = navigation.createEl("nav", { attr: { "aria-label": "Graph breadcrumbs" } });
        this.counts = navigation.createSpan({ cls: "trace-counts" });
        this.message = root.createDiv({ cls: "trace-message", attr: { role: "status", hidden: "" } });
        const main = root.createDiv({ cls: "trace-graph-main" });
        this.viewport = main.createDiv({ cls: "trace-viewport", attr: { tabindex: "0", "aria-label": "Code graph canvas" } });
        this.canvas = this.viewport.createDiv({ cls: "trace-canvas" });
        this.inspector = main.createEl("aside", { cls: "trace-inspector", attr: { "aria-label": "Selected entity" } });
        this.installPan();
        await this.loadGraph();
    }

    async onClose() {
        this.epoch++;
        this.loadEpoch++;
        this.searchEpoch++;
        this.linkPage = 0;
        this.data = undefined;
        this.adapter = undefined;
        this.contentEl.empty();
    }

    private installPan() {
        let drag: { id: number; x: number; y: number; left: number; top: number } | undefined;
        this.viewport.addEventListener("pointerdown", (event) => {
            if (event.button !== 0 || (event.target as Element).closest("button")) return;
            drag = { id: event.pointerId, x: event.clientX, y: event.clientY, left: this.viewport.scrollLeft, top: this.viewport.scrollTop };
            this.viewport.setPointerCapture(event.pointerId);
            this.viewport.addClass("is-panning");
        });
        this.viewport.addEventListener("pointermove", (event) => {
            if (!drag || drag.id !== event.pointerId) return;
            this.viewport.scrollLeft = drag.left + drag.x - event.clientX;
            this.viewport.scrollTop = drag.top + drag.y - event.clientY;
        });
        this.viewport.addEventListener("lostpointercapture", () => {
            drag = undefined;
            this.viewport.removeClass("is-panning");
        });
    }

    private save() { this.app.workspace.requestSaveLayout(); }
    private changed() { this.save(); void this.render(); }
    private say(text: string) { this.message.setText(text); this.message.hidden = !text; }

    private async loadGraph() {
        const adapter = this.adapter;
        if (!adapter) return;
        const token = ++this.loadEpoch;
        this.epoch++;
        this.searchEpoch++;
        this.results.hidden = true;
        this.data = undefined;
        this.canvas.empty();
        this.inspector.empty();
        this.breadcrumbs.empty();
        this.counts.empty();
        this.say("Loading TRACE snapshot…");
        try {
            const listing = await adapter.exists(DATA) ? await adapter.list(DATA) : { folders: [] };
            if (token !== this.loadEpoch) return;
            const projects = listing.folders.map((path) => path.split("/").pop() ?? "").filter((id) => PROJECT.test(id)).sort();
            this.project.empty();
            for (const id of projects) this.project.createEl("option", { text: id.slice(0, -17), value: id });
            if (!projects.length) {
                this.say("Export a project with trace obsidian --graph, then refresh this view.");
                return;
            }
            if (!projects.includes(this.state.project)) this.state = initial(projects[0]);
            this.project.value = this.state.project;
            const data = await Snapshot.open(adapter, this.state.project);
            if (token !== this.loadEpoch) return;
            this.data = data;
            this.cross.checked = this.state.cross;
            await this.render();
        } catch (error) {
            if (token === this.loadEpoch) this.say(error instanceof Error ? error.message : "The graph could not be read.");
        }
    }

    private zoom(delta: number) {
        this.state.zoom = delta ? Math.max(0.4, Math.min(1.6, this.state.zoom + delta)) : 1;
        this.changed();
    }

    private async search(raw: string) {
        const data = this.data;
        const query = raw.trim().replace(/\\/g, "/");
        const token = ++this.searchEpoch;
        this.results.empty();
        this.results.hidden = !query;
        if (!query || !data) return;
        this.results.createSpan({ text: "Searching…" });
        try {
            const found = await data.search(query);
            if (token !== this.searchEpoch) return;
            this.results.empty();
            control(this.results, "Close", () => { this.results.hidden = true; });
            if (!found.length) this.results.createSpan({ text: "No matching symbols or paths." });
            for (const node of found) {
                const button = control(this.results, `${entityLabel(node, data.meta.name)} · ${node.kind}`, () => this.focus(node.id));
                button.createSpan({ text: node.line ? `${node.path}:${node.line}` : node.path, cls: "trace-result-path" });
            }
        } catch (error) {
            if (token === this.searchEpoch) this.results.setText(error instanceof Error ? error.message : "Search failed.");
        }
    }

    private select(id: string) {
        this.state.selected = id;
        this.linkPage = 0;
        this.changed();
    }

    private focus(id: string) {
        this.state.root = id;
        this.results.hidden = true;
        this.viewport.scrollTo(0, 0);
        this.select(id);
    }

    private toggle(id: string) {
        const expanded = new Set(this.state.expanded);
        if (expanded.has(id)) expanded.delete(id);
        else expanded.add(id);
        this.state.expanded = [...expanded];
        this.changed();
    }

    private async layout(data: Snapshot, root: Entity, token: number) {
        const visible: Positioned[] = [];
        const controls: { id: string; offset: number; label: string; x: number; y: number }[] = [];
        const seen = new Set<string>();
        const expanded = new Set(this.state.expanded);
        const stack: ({ node: Entity; parent?: string; depth: number } |
            { id: string; offset: number; label: string; depth: number })[] = [{ node: root, depth: 0 }];
        let row = 0;
        while (stack.length && row < MAX_VISIBLE) {
            if (token !== this.epoch) break;
            const entry = stack.pop()!;
            const x = 24 + DX * entry.depth;
            const y = 24 + DY * row;
            if ("offset" in entry) {
                controls.push({ ...entry, x, y });
                row++;
                continue;
            }
            if (seen.has(entry.node.id)) continue;
            seen.add(entry.node.id);
            visible.push({ ...entry, x, y });
            row++;
            if (!entry.node.children || !expanded.has(entry.node.id)) continue;
            const branch = await data.branch(entry.node.id);
            const children = branch?.children ?? [];
            const offset = Math.min(this.pages.get(entry.node.id) ?? 0, Math.max(0, Math.floor((children.length - 1) / PAGE) * PAGE));
            const end = Math.min(children.length, offset + PAGE);
            const depth = entry.depth + 1;
            if (end < children.length) stack.push({ id: entry.node.id, offset: end, label: "Next children →", depth });
            for (let index = end - 1; index >= offset; index--) {
                stack.push({ node: children[index], parent: entry.node.id, depth });
            }
            if (offset > 0) stack.push({ id: entry.node.id, offset: offset - PAGE, label: "← Previous children", depth });
        }
        return { visible, controls, clipped: stack.length > 0 };
    }

    private async ancestry(data: Snapshot, selected: Entity, token: number) {
        const path: Entity[] = [selected];
        const seen = new Set([selected.id]);
        let parent = selected.parent;
        while (parent && !seen.has(parent) && token === this.epoch) {
            seen.add(parent);
            const branch = await data.branch(parent);
            if (!branch) break;
            path.push(branch.node);
            parent = branch.node.parent;
        }
        return path.reverse();
    }

    private async render() {
        const data = this.data;
        if (!data) return;
        const token = ++this.epoch;
        try {
            const root = (await data.branch(this.state.root) ?? await data.branch(data.meta.root))?.node;
            if (!root) throw new Error("The graph root is missing.");
            const selected = (await data.branch(this.state.selected))?.node ?? root;
            if (token !== this.epoch) return;
            this.state.root = root.id;
            this.state.selected = selected.id;
            const [layout, ancestry, links] = await Promise.all([
                this.layout(data, root, token), this.ancestry(data, selected, token), data.links(selected.id),
            ]);
            if (token !== this.epoch) return;
            const focus = (this.contentEl.ownerDocument.activeElement as HTMLElement | null)?.dataset.traceFocus;
            this.draw(data, selected, layout, links);
            this.details(data, selected, links);
            this.breadcrumbs.empty();
            for (const node of ancestry) {
                control(this.breadcrumbs, entityLabel(node, data.meta.name), () => this.focus(node.id));
            }
            if (focus) {
                const button = Array.from(this.contentEl.querySelectorAll<HTMLElement>("[data-trace-focus]"))
                    .find((element) => element.dataset.traceFocus === focus);
                button?.focus({ preventScroll: true });
            }
            this.counts.setText(`${layout.visible.length} shown · ${data.meta.nodeCount.toLocaleString()} entities · ${data.meta.edgeCount.toLocaleString()} links`);
            this.zoomLabel.setText(`${Math.round(this.state.zoom * 100)}%`);
            this.say(layout.clipped ? "View limit reached. Select a node and use Focus branch to continue." : "");
        } catch (error) {
            if (token === this.epoch) this.say(error instanceof Error ? error.message : "The graph could not be displayed.");
        }
    }

    private draw(data: Snapshot, selected: Entity, layout: Awaited<ReturnType<GraphView["layout"]>>, links: Links) {
        const { visible, controls } = layout;
        const nodes = new Map(visible.map((entry) => [entry.node.id, entry]));
        const width = Math.max(...[...visible, ...controls].map((entry) => entry.x)) + WIDTH + 36;
        const height = Math.max(...[...visible, ...controls].map((entry) => entry.y)) + HEIGHT + 36;
        this.canvas.empty();
        this.canvas.style.width = `${width * this.state.zoom}px`;
        this.canvas.style.height = `${height * this.state.zoom}px`;
        const world = this.canvas.createDiv({ cls: "trace-world" });
        world.style.width = `${width}px`;
        world.style.height = `${height}px`;
        world.style.transform = `scale(${this.state.zoom})`;
        const doc = world.ownerDocument;
        const svg = world.appendChild(doc.createElementNS(SVG, "svg"));
        svg.classList.add("trace-wires");
        svg.setAttribute("width", String(width));
        svg.setAttribute("height", String(height));
        svg.setAttribute("aria-hidden", "true");
        const arrowId = `trace-link-${crypto.randomUUID()}`;
        const marker = svg.appendChild(doc.createElementNS(SVG, "defs")).appendChild(doc.createElementNS(SVG, "marker"));
        for (const [name, value] of Object.entries({ id: arrowId, viewBox: "0 0 8 8", refX: "7", refY: "4",
            markerWidth: "6", markerHeight: "6", orient: "auto" })) marker.setAttribute(name, value);
        marker.appendChild(doc.createElementNS(SVG, "path")).setAttribute("d", "M0 0L8 4L0 8Z");
        const wire = (from: Positioned, to: Positioned, cross: boolean) => {
            const start = from.x + WIDTH;
            const end = to.x;
            const y1 = from.y + HEIGHT / 2;
            const y2 = to.y + HEIGHT / 2;
            const bend = cross ? Math.max(start, end) + 30 : (start + end) / 2;
            const path = svg.appendChild(doc.createElementNS(SVG, "path"));
            path.setAttribute("d", `M ${start} ${y1} C ${bend} ${y1} ${bend} ${y2} ${end} ${y2}`);
            path.classList.add(cross ? "trace-wire-cross" : "trace-wire-tree");
            if (cross) path.setAttribute("marker-end", `url(#${arrowId})`);
        };
        for (const entry of visible) {
            const parent = entry.parent ? nodes.get(entry.parent) : undefined;
            if (parent) wire(parent, entry, false);
        }
        const center = nodes.get(selected.id);
        if (center && this.state.cross) {
            for (const [list, inbound] of [[links.incoming, true], [links.outgoing, false]] as const) {
                const drawn = new Set<string>();
                for (const [id] of list) {
                    const peer = nodes.get(id);
                    if (!peer || peer === center || drawn.has(id)) continue;
                    drawn.add(id);
                    wire(inbound ? peer : center, inbound ? center : peer, true);
                }
            }
        }
        for (const entry of visible) {
            const node = entry.node;
            const selectedHere = node.id === selected.id;
            const label = entityLabel(node, data.meta.name);
            const card = world.createDiv({ cls: `trace-node${GROUPS.has(node.kind) ? " is-group" : ""}${selectedHere ? " is-selected" : ""}` });
            card.style.left = `${entry.x}px`;
            card.style.top = `${entry.y}px`;
            const select = control(card, label, () => this.select(node.id), { cls: "trace-node-label" });
            select.title = node.path;
            select.setAttribute("aria-pressed", String(selectedHere));
            select.dataset.traceFocus = `select-${node.id}`;
            select.createSpan({ cls: "trace-kind", text: node.kind });
            if (node.children) {
                const open = this.state.expanded.includes(node.id);
                const toggle = control(card, open ? "−" : "+", () => this.toggle(node.id), {
                    cls: "trace-node-toggle", title: `${open ? "Collapse" : "Expand"} ${label}`,
                });
                toggle.dataset.traceFocus = `toggle-${node.id}`;
                toggle.setAttribute("aria-expanded", String(open));
            }
        }
        for (const entry of controls) {
            const button = control(world, entry.label, () => {
                this.pages.set(entry.id, entry.offset);
                this.changed();
            }, { cls: "trace-more" });
            button.style.left = `${entry.x}px`;
            button.style.top = `${entry.y}px`;
        }
    }

    private details(data: Snapshot, node: Entity, links: Links) {
        const panel = this.inspector;
        panel.empty();
        panel.createSpan({ cls: "trace-kind", text: node.kind });
        panel.createEl("h3", { text: entityLabel(node, data.meta.name) });
        if (node.path !== ".") panel.createDiv({ cls: "trace-detail-path", text: node.line ? `${node.path}:${node.line}` : node.path });
        const actions = panel.createDiv({ cls: "trace-actions" });
        if (node.parent) control(actions, "Go to parent", () => this.focus(node.parent!));
        if (this.state.root !== node.id) control(actions, "Focus branch", () => this.focus(node.id));
        if (node.openable) {
            control(actions, "Open source", () => { void this.source(node, node.line); });
            control(actions, "Source folder", () => this.chooseSourceFolder(this.state.project));
        }
        if (data.meta.history) {
            const history = panel.createEl("details", { cls: "trace-history" });
            history.createEl("summary", { text: "Engineering history" });
            history.addEventListener("toggle", () => {
                if (history.open && history.children.length === 1) void this.showHistory(history, data, node.id);
            });
        }
        if (!this.state.cross) return;
        const length = Math.max(links.incoming.length, links.outgoing.length);
        const offset = Math.min(this.linkPage * PAGE, Math.max(0, Math.floor((length - 1) / PAGE) * PAGE));
        for (const [title, list] of [["Incoming", links.incoming], ["Outgoing", links.outgoing]] as const) {
            if (!list.length) continue;
            panel.createEl("h4", { text: `${title} · ${list.length}` });
            const section = panel.createDiv({ cls: "trace-link-list" });
            for (const [id, kind, confidence, path, line, column, openable, peerName] of list.slice(offset, offset + PAGE)) {
                const row = section.createDiv({ cls: "trace-reference" });
                const relationship = kind.replaceAll("_", " ").toLowerCase();
                control(row, `${relationship} · ${peerName}`, () => this.focus(id),
                    { title: `Follow ${relationship} to ${peerName}` });
                const label = line ? `${path}:${line}${column ? `:${column}` : ""}` : path;
                if (line && openable) control(row, label, () => { void this.source({ ...node, path, openable }, line); }, { cls: "trace-site" });
                else row.createSpan({ text: label, cls: "trace-site" });
                row.createSpan({ text: confidence, cls: "trace-kind" });
            }
        }
        if (length > PAGE) {
            const pager = panel.createDiv({ cls: "trace-actions" });
            if (offset > 0) control(pager, "Previous links", () => { this.linkPage--; this.changed(); });
            if (offset + PAGE < length) control(pager, "Next links", () => { this.linkPage++; this.changed(); });
        }
    }

    private async showHistory(container: HTMLDetailsElement, data: Snapshot, id: string) {
        const body = container.createDiv({ text: "Loading history…" });
        try {
            const events = await data.history(id);
            body.empty();
            if (!events.length) {
                body.setText("No engineering records are attached to this entity.");
                return;
            }
            const groups = [
                ["Investigations and findings", events.filter((event) => event.type !== "git_change")],
                ["Transcript evidence", events.filter((event) => event.evidence.some((item) => item.type === "transcript"))],
                ["Git changes", events.filter((event) => event.type === "git_change" || event.evidence.some((item) => item.type === "git"))],
            ] as const;
            for (const [label, items] of groups) {
                if (!items.length) continue;
                const group = body.createEl("details");
                group.createEl("summary", { text: `${label} · ${items.length}` });
                for (const item of items) {
                    const event = group.createEl("details");
                    const type = item.relationship.replaceAll("_", " ").toLowerCase();
                    event.createEl("summary", { text: `#${item.id} · ${type} · ${item.supersededBy ? "superseded" : item.status}` });
                    event.createDiv({ text: `Snapshot freshness · ${item.freshness}` });
                    if (item.supersededBy) event.createDiv({ text: `Replaced by event #${item.supersededBy}` });
                    this.historyCommand(event, `trace graph --event ${item.id}`);
                    for (const source of item.evidence) {
                        if ((label === "Transcript evidence" && source.type !== "transcript")
                            || (label === "Git changes" && source.type !== "git")) continue;
                        const evidence = event.createEl("details");
                        const location = source.type === "git" ? `${source.commit?.slice(0, 12)} · ${source.path}` : `Message #${source.message_id}`;
                        evidence.createEl("summary", { text: location });
                        if (source.availability !== "available") evidence.createDiv({ text: "Source unavailable at export." });
                        if (source.type === "git") evidence.createDiv({ text: `Snapshot freshness · ${source.freshness}` });
                        if (source.reference && /^ev_[0-9a-f]{32}$/.test(source.reference)) {
                            this.historyCommand(evidence, `trace evidence ${source.reference}`);
                        }
                    }
                }
            }
        } catch {
            body.setText("History could not be loaded. Refresh the snapshot.");
        }
    }

    private historyCommand(parent: HTMLElement, command: string) {
        const row = parent.createDiv({ cls: "trace-history-command" });
        row.createEl("code", { text: command });
        control(row, "Copy", () => {
            void row.ownerDocument.defaultView?.navigator.clipboard.writeText(command)
                .then(() => new Notice("TRACE command copied. Run it from the project."), () => new Notice("Copy the displayed command."));
        }, { title: "Copy TRACE command" });
    }

    private dialog(title: string): HTMLDialogElement {
        const dialog = this.contentEl.createEl("dialog", { cls: "trace-source-modal", attr: { "aria-label": title } });
        const header = dialog.createDiv({ cls: "trace-dialog-header" });
        header.createEl("h3", { text: title });
        control(header, "Close", () => dialog.close());
        dialog.addEventListener("close", () => dialog.remove(), { once: true });
        dialog.addEventListener("keydown", (event) => {
            if (event.key === "Escape") { event.stopPropagation(); dialog.close(); }
        });
        return dialog;
    }

    private chooseSourceFolder(project: string, done?: () => void) {
        const dialog = this.dialog("Connect the source folder");
        dialog.createEl("p", { text: "Choose the local repository root. TRACE reads a file only when you open it." });
        const form = dialog.createEl("form", { cls: "trace-source-form" });
        const input = form.createEl("input", { type: "text", attr: { "aria-label": "Local repository folder" } });
        input.value = this.plugin.sourceRoot(project);
        const button = form.createEl("button", { text: "Connect", attr: { type: "submit" } });
        const status = dialog.createDiv({ attr: { role: "status" } });
        form.addEventListener("submit", (event) => {
            event.preventDefault();
            button.disabled = true;
            void this.plugin.connectSource(project, input.value).then(() => {
                if (!dialog.open) return;
                dialog.close();
                done?.();
            }).catch((error: Error) => { status.setText(error.message); button.disabled = false; });
        });
        dialog.showModal();
        input.focus();
    }

    private async source(node: Entity, line: number | null) {
        const project = this.state.project;
        if (!this.plugin.sourceRoot(project)) {
            this.chooseSourceFolder(project, () => { void this.source(node, line); });
            return;
        }
        const dialog = this.dialog(node.path);
        const status = dialog.createDiv({ text: "Opening source…", attr: { role: "status" } });
        dialog.showModal();
        try {
            const lines = (await this.plugin.readSource(project, node)).split(/\r?\n/);
            if (!dialog.open) return;
            const focus = Math.min(lines.length, Math.max(1, line ?? 1));
            const from = Math.max(1, focus - 70);
            const to = Math.min(lines.length, focus + 130);
            status.setText(`Current working file · lines ${from}–${to} of ${lines.length}`);
            const pre = dialog.createEl("pre", { cls: "trace-source" });
            let highlighted: HTMLElement | undefined;
            for (let number = from; number <= to; number++) {
                const row = pre.createDiv({ cls: number === focus ? "trace-source-line is-current" : "trace-source-line" });
                row.createSpan({ cls: "trace-source-number", text: String(number) });
                row.createSpan({ text: lines[number - 1] });
                if (number === focus) highlighted = row;
            }
            highlighted?.scrollIntoView({ block: "center" });
        } catch (error) {
            status.setText(error instanceof Error ? error.message : "The source file could not be opened.");
        }
    }
}

export = TraceGraphPlugin;
