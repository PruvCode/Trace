// Only the trusted host script sees filesystem or Node APIs. Candidate code runs in WASM.
const fs = require('node:fs');
const { createRequire } = require('node:module');
const { randomBytes } = require('node:crypto');

const [dependencyRoot, typescriptPath] = process.argv.slice(2);
const { getQuickJS, shouldInterruptAfterDeadline } = createRequire(dependencyRoot + '/package.json')('quickjs-emscripten');
const ts = require(typescriptPath);

function parse(source) {
  const file = ts.createSourceFile('fixture.ts', source, ts.ScriptTarget.ESNext, true, ts.ScriptKind.TS);
  if (file.parseDiagnostics.length) throw new Error('TypeScript syntax error');
  return file;
}

function patch(source, replacements) {
  const file = parse(source);
  const edits = [];
  for (const [name, body] of Object.entries(replacements)) {
    if (typeof body !== 'string' || body.length > 2500) throw new Error('Invalid body');
    const replacement = parse(`function fixture() {\n${body}\n}`);
    if (replacement.statements.length !== 1 || !ts.isFunctionDeclaration(replacement.statements[0])) {
      throw new Error('Body left its function');
    }
    const definitions = file.statements.filter(node => ts.isFunctionDeclaration(node) && node.name?.text === name);
    if (definitions.length !== 1 || !definitions[0].body) throw new Error('Ambiguous definition');
    const original = definitions[0].body;
    edits.push([original.getStart(file), original.end, replacement.statements[0].body.getText(replacement)]);
  }
  for (const [start, end, body] of edits.sort((a, b) => b[0] - a[0])) {
    source = source.slice(0, start) + body + source.slice(end);
  }
  return source;
}

function scriptFor(source, fixtures) {
  const file = parse(source);
  const edits = [];
  for (const statement of file.statements) {
    if (ts.isImportDeclaration(statement)) {
      if (fixtures === null || statement.moduleSpecifier.text !== '@/config/hero') throw new Error('Unknown import');
      edits.push([statement.getStart(file), statement.end, `const HERO_STAGES = ${JSON.stringify(fixtures)};`]);
    }
    for (const modifier of statement.modifiers ?? []) {
      if (modifier.kind === ts.SyntaxKind.ExportKeyword) edits.push([modifier.getStart(file), modifier.end, '']);
    }
  }
  for (const [start, end, replacement] of edits.sort((a, b) => b[0] - a[0])) {
    source = source.slice(0, start) + replacement + source.slice(end);
  }
  return ts.transpileModule(source, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.None } }).outputText;
}

async function main() {
  const input = JSON.parse(fs.readFileSync(0, 'utf8'));
  if (input.definitions) {
    const file = parse(input.source);
    const wanted = new Set(input.definitions);
    const selected = file.statements.filter(node => ts.isFunctionDeclaration(node) && wanted.has(node.name?.text));
    if (selected.length !== wanted.size) throw new Error('Missing or ambiguous definition');
    console.log(JSON.stringify({ source: selected.map(node => node.getText(file)).join('\n\n') }));
    return;
  }
  const vm = await getQuickJS();
  const options = { memoryLimitBytes: 8 * 1024 * 1024, maxStackSizeBytes: 256 * 1024 };
  function evaluate(script) {
    return vm.evalCode(script, { ...options, shouldInterrupt: shouldInterruptAfterDeadline(Date.now() + 500) });
  }
  if (input.boundary) {
    if (!evaluate('typeof process === "undefined" && typeof require === "undefined" && typeof fetch === "undefined"')) {
      throw new Error('Unexpected host access');
    }
    try {
      evaluate('while (true) {}');
    } catch (error) {
      if (!String(error.message ?? error).includes('interrupted')) throw error;
      console.log(JSON.stringify({ contained: true }));
      return;
    }
    throw new Error('Loop was not interrupted');
  }
  const nonce = randomBytes(16).toString('hex');
  const checks = input.checks.map(([expression, expected], index) => {
    const expectedText = JSON.stringify(expected);
    const failure = `check ${index + 1}`;
    return `{
      const observed = JSON.stringify(${expression});
      if (observed !== ${JSON.stringify(expectedText)}) {
        throw new Error(${JSON.stringify(failure)}${input.diagnostics ? ' + ": " + String(observed).slice(0, 180)' : ''});
      }
    }`;
  });
  try {
    const source = patch(input.source, input.replacements);
    const output = evaluate(`${scriptFor(source, input.heroStages)}\n${checks.join('\n')}\n${JSON.stringify(nonce)}`);
    console.log(JSON.stringify({ passed: output === nonce }));
  } catch (error) {
    const message = error?.message;
    const match = typeof message === 'string' && /^check (\d+)(?:: ([\s\S]{0,180}))?$/.exec(message);
    const check = match ? `check ${match[1]}` : 'invalid edit or execution error';
    if (input.diagnostics && match && match[2] !== undefined) {
      const [expression, expected] = input.checks[Number(match[1]) - 1] ?? [];
      if (expression) {
        console.log(JSON.stringify({ passed: false, check, expression, expected, observed: match[2] }));
        return;
      }
    }
    console.log(JSON.stringify({ passed: false, check }));
  }
}

main().catch(error => { console.error(error.message); process.exitCode = 1; });
