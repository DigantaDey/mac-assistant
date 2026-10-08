"""Accessibility tree bounds and native-element activation behavior."""

from __future__ import annotations

import sys
import time
from types import SimpleNamespace

from aura.ax import MacAXTree


class Element:
    def __init__(self, name: str) -> None:
        self.name = name


class FakeApplicationServices:
    def __init__(self) -> None:
        self.root = Element("application")
        self.button = Element("button")
        self.attributes = {
            self.root: {
                "AXRole": "AXApplication",
                "AXTitle": "Test App",
                "AXChildren": [self.button],
            },
            self.button: {
                "AXRole": "AXButton",
                "AXTitle": "Continue",
                "AXPosition": (10, 20),
                "AXSize": (21, 9),
                "AXChildren": [],
            },
        }
        self.calls: list[tuple[str, str]] = []
        self.actions: list[tuple[Element, str]] = []
        self.action_names = ["AXPress"]
        self.timeout: tuple[Element, float] | None = None

    def AXUIElementCreateApplication(self, pid: int) -> Element:
        self.calls.append(("create", str(pid)))
        return self.root

    def AXUIElementSetMessagingTimeout(self, element: Element, timeout: float) -> int:
        self.timeout = (element, timeout)
        return 0

    def AXUIElementCopyAttributeValue(self, element: Element, name: str, _error):
        self.calls.append((element.name, name))
        return 0, self.attributes.get(element, {}).get(name)

    def AXUIElementCopyActionNames(self, element: Element, _error):
        self.calls.append((element.name, "AXActions"))
        return 0, self.action_names

    def AXUIElementPerformAction(self, element: Element, action: str) -> int:
        self.actions.append((element, action))
        return 0


def make_tree(monkeypatch) -> tuple[MacAXTree, FakeApplicationServices]:
    services = FakeApplicationServices()
    appkit = SimpleNamespace(NSWorkspace=object())
    monkeypatch.setitem(sys.modules, "AppKit", appkit)
    monkeypatch.setitem(sys.modules, "ApplicationServices", services)
    return MacAXTree(pid=123), services


def test_tree_nodes_keep_native_element_and_press_without_rescan(monkeypatch):
    tree, services = make_tree(monkeypatch)
    root = tree.root()
    button = root.children[0]

    assert root.native_ref is services.root
    assert button.native_ref is services.button
    assert button.label == "Continue"
    before_press = list(services.calls)

    assert tree.press(button) is True
    assert services.actions == [(services.button, "AXPress")]
    # Pressing uses the retained AXUIElement, not a second recursive root read.
    assert services.calls.count(("create", "123")) == 1
    assert services.calls.count(("application", "AXChildren")) == before_press.count(
        ("application", "AXChildren"))
    assert services.timeout == (services.root, tree.MESSAGING_TIMEOUT_SECONDS)


def test_native_geometry_is_loaded_only_for_pointer_fallback(monkeypatch):
    tree, services = make_tree(monkeypatch)
    services.action_names = []
    posted: list[tuple[int, tuple[int, int]]] = []

    quartz = SimpleNamespace(
        kCGEventLeftMouseDown=1,
        kCGEventLeftMouseUp=2,
        kCGHIDEventTap=3,
        CGEventCreateMouseEvent=lambda _source, kind, point, _button: (kind, point),
        CGEventPost=lambda _tap, event: posted.append(event),
    )
    monkeypatch.setitem(sys.modules, "Quartz", quartz)
    button = tree.root().children[0]

    assert tree.press(button) is True
    assert posted == [(1, (20, 24)), (2, (20, 24))]


def test_tree_stops_expanding_when_its_budget_expires(monkeypatch):
    tree, services = make_tree(monkeypatch)
    tree.TREE_BUDGET_SECONDS = 0.001
    original = services.AXUIElementCopyAttributeValue

    def slow_children(element: Element, name: str, error):
        if element is services.root and name == "AXChildren":
            time.sleep(0.01)
        return original(element, name, error)

    services.AXUIElementCopyAttributeValue = slow_children
    root = tree.root()

    assert root.role == "application"
    assert root.children == []
    assert not any(element == "button" for element, _ in services.calls)
