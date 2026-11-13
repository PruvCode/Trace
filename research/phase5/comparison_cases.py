"""Frozen qualification cases and held-out single-function engineering tasks."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Task:
    name: str
    source: str
    request: str
    decision: str
    markers: tuple[str, ...]
    cases: tuple[tuple[tuple, object, object], ...]

    @property
    def file(self):
        return f"{self.name}.py"

    @property
    def task(self):
        return self.request

    @property
    def review(self):
        return self.decision

    def check(self, prior=False):
        tests = self.cases[:1] if prior else self.cases
        calls = [
            f"assert_result(scope[{self.name!r}](*{args!r}), {(old if prior else expected)!r})"
            for args, expected, old in tests
        ]
        prefix = (
            'scope = {"__name__": "candidate"}\nexec(source, scope)\n'
            'def assert_result(actual, expected):\n'
            '    assert actual == expected and type(actual) is type(expected)\n'
        )
        return prefix + "\n".join(calls)


QUALIFICATION = (
    Task(
        "resize_limit", "def resize_limit(width):\n    return min(100, width)\n",
        "Update resize_limit to obey the design limit while preserving integer inputs.",
        "Design decision for resize_limit: clamp width into the inclusive range 12 through 72.",
        ("12", "72"),
        (((3,), 12, 3), ((84,), 72, 84), ((34,), 34, 34), ((12,), 12, 12)),
    ),
    Task(
        "normalize_order", 'def normalize_order(order):\n    return {"id": order["id"]}\n',
        "Implement the agreed normalize_order output shape.",
        "normalize_order must return exactly these keys: order_id copied from id, and status set to 'pending'.",
        ("order_id", "pending"),
        ((({"id": 2},), {"order_id": 2, "status": "pending"}, {"id": 2}),
         (({"id": "x"},), {"order_id": "x", "status": "pending"}, {"id": "x"})),
    ),
    Task(
        "report_allowed", "def report_allowed(days, archived=False):\n    return days < 7\n",
        "Apply the approved report_allowed access rule. Keep the same arguments.",
        "report_allowed returns True only when archived is false and days is at most 14, including day 14.",
        ("14", "archived"),
        (((14, False), True, False), ((15, False), False, False), ((2, True), False, True)),
    ),
)

HELD = (
    Task(
        "delivery_delays",
        "def delivery_delays(failures):\n    return [2 ** i for i in range(failures)]\n",
        "Update delivery_delays to implement the backoff decision from the delivery investigation.",
        "Delivery investigation decision for delivery_delays and backoff: start at 2 seconds and triple each step. "
        "Return one delay per failure. Zero or negative failures return an empty list.",
        ("2 seconds", "triple"),
        (((3,), [2, 6, 18], [1, 2, 4]), ((0,), [], []), ((-2,), [], []), ((1,), [2], [1])),
    ),
    Task(
        "export_row",
        'def export_row(item):\n    return {"id": item["id"], "title": item["title"]}\n',
        "Update export_row to follow the approved export wire contract. Preserve its argument.",
        "Approved export_row wire contract: output exactly record_id from item['id'], caption from item['title'], "
        "and contract_level set to integer 7. Do not return the old keys.",
        ("record_id", "caption", "contract_level"),
        ((({"id": "u", "title": "Café"},), {"record_id": "u", "caption": "Café", "contract_level": 7},
          {"id": "u", "title": "Café"}),
         (({"id": 0, "title": ""},), {"record_id": 0, "caption": "", "contract_level": 7}, {"id": 0, "title": ""})),
    ),
    Task(
        "can_purge", "def can_purge(age_days, on_hold=False):\n    return age_days > 30\n",
        "Update can_purge to match the retention and legal hold decision.",
        "Retention decision for can_purge: eligible at 45 days or older, except a row on legal hold is never eligible.",
        ("45 days", "never eligible"),
        (((44, False), False, True), ((45, False), True, True), ((90, True), False, True), ((-1,), False, False)),
    ),
    Task(
        "accept_batch", "def accept_batch(count, trusted=False):\n    return count <= 10\n",
        "Apply the approved accept_batch limits for ordinary and trusted callers.",
        "The accept_batch decision sets an inclusive limit of 8 ordinary items or 12 trusted items. "
        "In both modes, count must be positive.",
        ("8 ordinary", "12 trusted"),
        (((10, False), False, True), ((8,), True, True), ((12, True), True, False),
         ((13, True), False, False), ((0, True), False, True)),
    ),
    Task(
        "normalize_tier",
        'def normalize_tier(value):\n    return {"cold": 1, "hot": 2}.get(value.strip().lower(), 1)\n',
        "Update normalize_tier to the approved tier codes. Keep case and whitespace handling.",
        "Approved normalize_tier codes: cold maps to 3, hot maps to 5, and unknown maps to 9. "
        "Keep the existing trim and lowercase normalization.",
        ("cold maps to 3", "hot maps to 5", "unknown maps to 9"),
        ((("HOT",), 5, 2), ((" cold ",), 3, 1), (("other",), 9, 1), (("",), 9, 1)),
    ),
    Task(
        "round_fee", "def round_fee(cents):\n    return min(100, cents)\n",
        "Update round_fee to apply the billing decision for fee bounds. Keep integer output.",
        "Billing decision for round_fee: nonpositive amounts return 0. Positive amounts are clamped "
        "to the inclusive range 5 through 75 cents.",
        ("5 through 75", "nonpositive"),
        (((100,), 75, 100), ((3,), 5, 3), ((-2,), 0, -2), ((0,), 0, 0), ((14,), 14, 14)),
    ),
)
