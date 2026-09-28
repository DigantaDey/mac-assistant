"""Accessibility (AX) — how Aura sees and acts inside *any* app, by label.

macOS exposes a structured tree of every UI element: buttons, links, text
fields, menus — each with a role, a label, a value, a position. Reading it is
~50 ms (100× faster than screenshots) and it is *ground truth*: a label comes
from the app itself, never from a model's imagination. This is the substrate
that lets Aura click "Sign In" without a vision model guessing at pixels.

Three implementations behind one interface:

  MacAXTree   — pyobjc over AXUIElement (Apple's own accessibility API);
                used automatically on a Mac with the Accessibility grant.
  MockAXTree  — a deterministic Safari-like window used by the demo profile,
                tests, and CI. Same interface, scripted tree.

Element picking (choosing WHICH node matches "the sign in button") lives in
aura/picker.py — coarse-to-fine, Laya-scored.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class AXNode:
    role: str                          # "button", "textfield", "link", …
    label: str = ""                    # visible name (title/description/value)
    value: str = ""                    # current contents (for fields)
    position: tuple[int, int] | None = None
    size: tuple[int, int] | None = None
    actions: tuple[str, ...] = ()      # e.g. ("AXPress",)
    children: list[AXNode] = field(default_factory=list)

    def flat(self) -> list[AXNode]:
        out: list[AXNode] = []
        stack = [self]
        while stack:
            node = stack.pop()
            out.append(node)
            stack.extend(reversed(node.children))
        return out

    def center(self) -> tuple[int, int] | None:
        if self.position and self.size:
            return (self.position[0] + self.size[0] // 2,
                    self.position[1] + self.size[1] // 2)
        return None


class AXUnavailable(RuntimeError):
    pass


# --------------------------------------------------------------------------- #
# Real Mac implementation (pyobjc over ApplicationServices)                    #
# --------------------------------------------------------------------------- #


class MacAXTree:
    """Reads and drives the frontmost app via the Accessibility API.

    Requires the `pyobjc-framework-ApplicationServices` package (part of the
    `[mac]` extra) and the user's Accessibility grant. All product logic is
    testable without this class — MockAXTree implements the same interface.
    """

    MAX_DEPTH = 12
    MAX_NODES = 400

    def __init__(self, pid: int | None = None) -> None:
        try:
            import ApplicationServices as AS  # type: ignore
            from AppKit import NSWorkspace  # type: ignore
        except Exception as exc:  # pragma: no cover - Mac only
            raise AXUnavailable(f"pyobjc unavailable: {exc}") from exc
        self._AS = AS
        if pid is None:
            app = NSWorkspace.sharedWorkspace().frontmostApplication()
            if app is None:
                raise AXUnavailable("no frontmost application")
            self.app_name = app.localizedName()
            pid = app.processIdentifier()
        self._ref = AS.AXUIElementCreateApplication(pid)

    # -- attribute helpers ------------------------------------------------ #

    def _attr(self, ref, name: str):
        AS = self._AS
        try:
            result = AS.AXUIElementCopyAttributeValue(ref, name, None)
        except Exception:
            return None
        # pyobjc may return the value directly or an (error, value) pair
        if isinstance(result, tuple):
            return result[-1] if result else None
        return result

    def _actions(self, ref) -> tuple[str, ...]:
        AS = self._AS
        try:
            names = AS.AXUIElementCopyActionNames(ref, None)
        except Exception:
            return ()
        if isinstance(names, tuple):
            names = names[-1] if names else None
        return tuple(names) if names else ()

    # -- tree -------------------------------------------------------------- #

    def root(self) -> AXNode:
        count = 0

        def convert(ref, depth: int) -> AXNode | None:
            nonlocal count
            if depth > self.MAX_DEPTH or count >= self.MAX_NODES:
                return None
            count += 1
            role = str(self._attr(ref, "AXRole") or "unknown").removeprefix("AX").lower()
            title = self._attr(ref, "AXTitle")
            desc = self._attr(ref, "AXDescription")
            value = self._attr(ref, "AXValue")
            label = str(title or desc or "").strip()
            value_str = str(value).strip() if isinstance(value, str) else ""
            if not label and value_str and role in ("textfield", "searchfield", "textarea"):
                label = value_str
            position = size = None
            try:
                pos = self._attr(ref, "AXPosition")
                siz = self._attr(ref, "AXSize")
                if pos and siz:
                    position = (tuple(pos) if not hasattr(pos, "x")
                                else (int(pos.x), int(pos.y)))
                    size = (tuple(siz) if not hasattr(siz, "x")
                            else (int(siz.width), int(siz.height)))
            except Exception:
                pass
            node = AXNode(role=role, label=label[:120], value=value_str[:200],
                          position=position, size=size, actions=self._actions(ref))
            children = self._attr(ref, "AXChildren")
            if children:
                for child in children:
                    sub = convert(child, depth + 1)
                    if sub:
                        node.children.append(sub)
            return node

        return convert(self._ref, 0) or AXNode(role="unknown")

    def front_app_name(self) -> str:
        return getattr(self, "app_name", "Unknown")

    # -- actions ------------------------------------------------------------ #

    def press(self, node: AXNode) -> bool:
        """Press a button-like element: AXPress when available, else a synthetic
        click at its center (CGEvent). Returns success."""
        AS = self._AS
        # The clean path is AXPress on the live element ref; we resolve the ref
        # again by walking to the same path in the real tree.
        ref = self._find_ref(node)
        if ref is not None and "AXPress" in self._actions(ref):
            try:
                err = AS.AXUIElementPerformAction(ref, "AXPress")
                return err == 0 or err is None
            except Exception:
                pass
        center = node.center()
        if center is None:
            return False
        return self._click_at(center)

    def _find_ref(self, node: AXNode):
        """Re-locate a node's live ref by role+label path (position-stable)."""
        target_path = _path_of(self.root(), node)
        if not target_path:
            return None
        ref = self._ref
        for index in target_path:
            try:
                children = self._attr(ref, "AXChildren") or []
                ref = children[index]
            except Exception:
                return None
        return ref

    def _click_at(self, point: tuple[int, int]) -> bool:
        try:
            import Quartz  # type: ignore
            x, y = point
            for kind in (Quartz.kCGEventLeftMouseDown, Quartz.kCGEventLeftMouseUp):
                event = Quartz.CGEventCreateMouseEvent(None, kind, (x, y), 0)
                Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)
            return True
        except Exception as exc:  # pragma: no cover - Mac only
            raise AXUnavailable(f"CGEvent click failed: {exc}") from exc

    def focus(self, node: AXNode) -> bool:
        AS = self._AS
        ref = self._find_ref(node)
        if ref is None:
            return False
        try:
            AS.AXUIElementSetAttributeValue(ref, "AXFocused", True)
            return True
        except Exception:
            return False

    def insert(self, node: AXNode, text: str) -> bool:
        """Focus the field and type. Keystrokes respect the focused element."""
        if not self.focus(node):
            return False
        import subprocess
        script = f'tell application "System Events" to keystroke {text!r}'
        proc = subprocess.run(["osascript", "-e", script],
                              capture_output=True, text=True, timeout=15)
        return proc.returncode == 0


def _path_of(root: AXNode, target: AXNode) -> list[int] | None:
    """Path of child indices from root to target (identity match)."""
    def walk(node: AXNode, path: list[int]) -> list[int] | None:
        if node is target:
            return path
        for i, child in enumerate(node.children):
            found = walk(child, path + [i])
            if found is not None:
                return found
        return None
    return walk(root, [])


# --------------------------------------------------------------------------- #
# Deterministic mock — the demo profile's window                               #
# --------------------------------------------------------------------------- #


class MockAXTree:
    """A scripted, believable Safari-like window with 25+ labeled elements —
    big enough to exercise coarse-to-fine chunking, small enough to read."""

    def __init__(self) -> None:
        self.log: list[tuple[str, str]] = []   # (action, detail)
        self.root_node = self._build()

    def front_app_name(self) -> str:
        return "Safari"

    def _build(self) -> AXNode:
        def btn(label, value=""):
            return AXNode("button", label=label, value=value, actions=("AXPress",),
                          size=(90, 28))

        def link(label):
            return AXNode("link", label=label, actions=("AXPress",), size=(110, 18))

        def field(label, value="", role="textfield"):
            return AXNode(role, label=label, value=value,
                          actions=("AXPress",), size=(220, 24))

        def text(label):
            return AXNode("statictext", label=label, size=(300, 16))

        return AXNode(
            role="window", label="GitHub — Build software better, together",
            size=(1200, 800),
            children=[
                AXNode("toolbar", label="Toolbar", children=[
                    btn("Back"), btn("New Tab"), btn("Share"),
                    field("Address and Search", value="https://github.com"),
                ]),
                AXNode("tabgroup", label="Tabs", children=[
                    btn("GitHub — Pull requests", value="tab"),
                    btn("Gmail — Inbox", value="tab"),
                ]),
                AXNode("group", label="Main content", children=[
                    text("The world's leading AI-powered developer platform."),
                    field("Search GitHub", value="", role="searchfield"),
                    link("Skip to content"),
                    link("Sign in"),
                    btn("Sign up for GitHub"),
                    text("Trusted by the world's largest organizations."),
                    AXNode("list", label="Favorites", children=[
                        link(name) for name in (
                            "Dashboard", "Pull requests", "Issues", "Codespaces",
                            "Marketplace", "Explore", "Copilot", "Dependabot",
                            "Actions", "Sponsors",
                        )
                    ]),
                    AXNode("group", label="Settings", children=[
                        btn("Save preferences"),
                        btn("Delete repository"),
                        field("Repository description", value="mac assistant"),
                    ]),
                ]),
            ],
        )

    # -- same interface as MacAXTree ---------------------------------------- #

    def root(self) -> AXNode:
        return self.root_node

    def press(self, node: AXNode) -> bool:
        if node.actions and "AXPress" in node.actions:
            self.log.append(("press", node.label))
            return True
        if node.center():
            self.log.append(("click", node.label))
            return True
        return False

    def focus(self, node: AXNode) -> bool:
        self.log.append(("focus", node.label))
        return True

    def insert(self, node: AXNode, text: str) -> bool:
        node.value = text
        self.log.append(("insert", f"{node.label} ← {text}"))
        return True
