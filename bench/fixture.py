"""Builds the small project the benchmark runs on: a big module, a noisy test
suite with one failing test, and room for small unrelated tasks.

    python3 fixture.py <dest>
"""

import os
import sys
import textwrap

N_FUNCS = 300          # inventory.py: ~3,000 lines
N_TESTS = 1200         # verbose unittest output: ~1,200 lines

# Values the tasks ask about (checked in run.py).
ANSWERS = {"restock_threshold": 17, "warehouse_capacity": 4800, "late_fee_rate": 0.035, "max_batch_size": 250}


def inventory_py():
    out = ['"""Inventory rules for a small shop. Generated for the cut-context benchmark."""', "", "import math", ""]
    specials = {
        40: ("restock_threshold", "default minimum stock before a reorder is triggered", "17"),
        110: ("warehouse_capacity", "how many units the main warehouse holds", "4800"),
        190: ("late_fee_rate", "fraction charged per day on late supplier invoices", "0.035"),
        260: ("max_batch_size", "largest number of units shipped in one batch", "250"),
    }
    for i in range(N_FUNCS):
        if i in specials:
            name, doc, val = specials[i]
            out += ["def %s():" % name, '    """Return the %s."""' % doc, "    return %s" % val, ""]
            continue
        out += [
            "def rule_%03d(qty, price, season=0):" % i,
            '    """Pricing rule %d: adjusts the price of an order line by season and quantity."""' % i,
            "    base = qty * price",
            "    if season %% 4 == %d:" % (i % 4),
            "        base *= 1.0%d" % (i % 10),
            "    if qty > %d:" % (10 + i % 50),
            "        base -= %d * 0.01 * base" % (i % 7),
            "    return round(base, 2)",
            "",
        ]
    out += [
        "def apply_discount(total, percent):",
        '    """Apply a percentage discount and round to cents."""',
        "    return math.floor(total * (1 - percent / 100))  # BUG: should round to cents, not whole units",
        "",
    ]
    return "\n".join(out) + "\n"


def tests_py():
    lines = ["import unittest", "import inventory", "", "", "class TestRules(unittest.TestCase):"]
    for i in range(N_TESTS):
        f = i % N_FUNCS
        if f in (40, 110, 190, 260):
            f += 1
        lines += ["    def test_rule_%04d(self):" % i,
                  "        self.assertIsInstance(inventory.rule_%03d(%d, 2.5, %d), float)" % (f, 1 + i % 30, i % 4)]
    lines += ["", "", "class TestDiscount(unittest.TestCase):",
              "    def test_discount_rounds_to_cents(self):",
              "        self.assertEqual(inventory.apply_discount(19.99, 10), 17.99)", ""]
    return "\n".join(lines) + "\n"


def build(dest):
    os.makedirs(os.path.join(dest, "tests"), exist_ok=True)
    files = {
        "inventory.py": inventory_py(),
        "tests/__init__.py": "",
        "tests/test_inventory.py": tests_py(),
        "utils.py": '"""Small helpers."""\n',
        "README.md": "# Shop inventory\n\nRules for pricing and stock.\n",
        "orders.csv": "id,sku,qty\n" + "".join("%d,SKU-%04d,%d\n" % (i, i % 97, 1 + i % 9) for i in range(400)),
    }
    for rel, text in files.items():
        with open(os.path.join(dest, rel), "w") as fh:
            fh.write(text)


if __name__ == "__main__":
    build(sys.argv[1])
    print(textwrap.dedent("built %s" % sys.argv[1]))
