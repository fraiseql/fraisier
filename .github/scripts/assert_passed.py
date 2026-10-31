"""Fail unless MODULE's tests in a JUnit report all passed, and there are COUNT.

usage: assert_passed.py JUNIT_XML MODULE COUNT
"""

import sys
import xml.etree.ElementTree as ET

report, module, expected = sys.argv[1], sys.argv[2], int(sys.argv[3])
cases = [
    case
    for case in ET.parse(report).iter("testcase")
    if case.get("classname", "").startswith(module)
]
not_passed = [
    f"{case.get('classname')}::{case.get('name')}: {child.tag}"
    for case in cases
    for child in case
    if child.tag in {"skipped", "failure", "error"}
]
passed = len(cases) - len({line.split(": ")[0] for line in not_passed})
print(f"{module}: {passed} of {expected} expected passed")
for line in not_passed:
    print(f"  {line}")
sys.exit(0 if passed == expected and not not_passed and len(cases) == expected else 1)
