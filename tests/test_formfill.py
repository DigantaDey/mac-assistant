"""Form filling — dictate into any browser form.

The v0.5.1 "power" feature, tested the way the product runs it: the live
tree is the ground truth, the parser never invents values, the skill reports
honestly, and the safety gate keeps a *sending* button one human tap away.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aura.ax import AXNode
from aura.formfill import llm_parse, plan_fill, scan_form
from aura.planner import MockPlanner
from aura.skills import build_default_registry
from aura.skills.base import SkillContext
from aura.skills.forms import AXDictate, AXFillForm, AXReadForm

# --------------------------------------------------------------------------- #
# A believable form to fill                                                    #
# --------------------------------------------------------------------------- #

class FormTree:
    """The frontmost browser window: a checkout form + a submit button."""

    def __init__(self) -> None:
        def field(label, value="", role="textfield"):
            return AXNode(role, label=label, value=value, size=(220, 24))

        self.root_node = AXNode(
            role="window", label="Store — Checkout", size=(1200, 800),
            children=[
                AXNode("group", label="Checkout form", children=[
                    AXNode("statictext", label="Shipping information"),
                    field("First Name"),
                    field("Last Name"),
                    field("Email"),
                    field("Phone", role="textfield"),
                    field("Street Address", role="textfield"),
                    field("City"),
                    field("Notes", role="textarea"),
                    AXNode("button", label="Place order", actions=("AXPress",)),
                ]),
            ],
        )
        self.log: list[tuple[str, str]] = []

    def front_app_name(self) -> str:
        return "Safari"

    def root(self) -> AXNode:
        return self.root_node

    def press(self, node: AXNode) -> bool:
        self.log.append(("press", node.label))
        return True

    def focus(self, node: AXNode) -> bool:
        return True

    def insert(self, node: AXNode, text: str) -> bool:
        node.value = text
        self.log.append(("insert", f"{node.label} ← {text}"))
        return True


class FakeBridge:
    platform = "fake"

    def __init__(self, tree: FormTree) -> None:
        self.tree = tree
        self.keystrokes: list[str] = []

    def ax_tree(self):
        return self.tree

    def keystroke(self, text: str):
        self.keystrokes.append(text)
        return True, f"[fake] typed {text!r}"


def _ctx(tree: FormTree = None, bridge=None):
    tree = tree or FormTree()
    bridge = bridge or FakeBridge(tree)
    cfg = SimpleNamespace(planner=SimpleNamespace(engine="mock", base_url="", model=""))
    return SkillContext(bridge=bridge, memory=None, config=cfg), tree, bridge


def _plan(planner: MockPlanner, transcript: str):
    return asyncio.run(planner.plan(transcript, {}))


@pytest.fixture()
def planner():
    return MockPlanner(build_default_registry().catalog_prompt())


# --------------------------------------------------------------------------- #
# Scanning                                                                      #
# --------------------------------------------------------------------------- #

class TestScanForm:
    def test_finds_fields_and_buttons(self):
        scan = scan_form(FormTree().root(), "Safari")
        assert scan.labels() == ["First Name", "Last Name", "Email", "Phone",
                                 "Street Address", "City", "Notes"]
        assert [b.label for b in scan.submit_buttons] == ["Place order"]

    def test_empty_screen(self):
        root = AXNode("window", label="blank", children=[AXNode("statictext", label="hi")])
        scan = scan_form(root, "Safari")
        assert scan.fields == [] and scan.submit_buttons == []


# --------------------------------------------------------------------------- #
# The heuristic parser — fast, grounded, never invents                          #
# --------------------------------------------------------------------------- #

class TestPlanFill:
    def _scan(self):
        return scan_form(FormTree().root(), "Safari")

    def test_label_grammar(self):
        out = plan_fill(
            "fill this form: first name John, last name Smith, email john@smith.com",
            self._scan())
        got = {a.field.label: a.value for a in out}
        assert got == {"First Name": "John", "Last Name": "Smith",
                       "Email": "john@smith.com"}
        assert all(a.how == "label" for a in out)

    def test_synonym_mail(self):
        out = plan_fill("the mail is j@x.com, phone is 555 0100", self._scan())
        got = {a.field.label: a.value for a in out}
        assert got == {"Email": "j@x.com", "Phone": "555 0100"}

    def test_set_to_grammar(self):
        out = plan_fill("set city to Portland", self._scan())
        assert {a.field.label: a.value for a in out} == {"City": "Portland"}

    def test_unmentioned_fields_stay_empty(self):
        out = plan_fill("name John only", self._scan())
        got = {a.field.label for a in out}
        assert got == {"First Name"}          # "name" matches first, not last…
        assert "Notes" not in got

    def test_nothing_matched(self):
        assert plan_fill("the weather is nice", self._scan()) == []

    def test_ordinal_fallback(self):
        out = plan_fill("first field One, second field Two", self._scan())
        got = {a.field.label: a.value for a in out}
        assert got.get("First Name") == "One"
        assert got.get("Last Name") == "Two"

    def test_bare_positional_list(self):
        out = plan_fill("fill this form: Alpha, Beta, Gamma", self._scan())
        got = {a.field.label: a.value for a in out}
        assert got == {"First Name": "Alpha", "Last Name": "Beta",
                       "Email": "Gamma"}
        assert all(a.how == "positional" for a in out)

    def test_values_keep_case_and_content(self):
        out = plan_fill("email User@Example.COM, notes Meet at 9 am", self._scan())
        got = {a.field.label: a.value for a in out}
        assert got["Email"] == "User@Example.COM"
        assert got["Notes"] == "Meet at 9 am"


# --------------------------------------------------------------------------- #
# The LLM pass — free paraphrase, but grounded                                 #
# --------------------------------------------------------------------------- #

class TestLlmParse:
    def _scan(self):
        return scan_form(FormTree().root(), "Safari")

    def test_valid_labels_accepted(self):
        def ask(prompt: str) -> str:
            return '{"fields": [{"label": "Email", "value": "x@y.z"},' \
                   ' {"label": "City", "value": "Reno"}]}'
        out = llm_parse("the mail is x at y dot z, city Reno", self._scan(), ask)
        assert {a.field.label: a.value for a in out} == {
            "Email": "x@y.z", "City": "Reno"}

    def test_hallucinated_labels_dropped(self):
        def ask(prompt: str) -> str:
            return '{"fields": [{"label": "Wizard Name", "value": "X"},' \
                   ' {"label": "City", "value": "Reno"}]}'
        out = llm_parse("whatever", self._scan(), ask)
        assert {a.field.label for a in out} == {"City"}

    def test_client_failure_is_none(self):
        def ask(prompt: str) -> str:
            raise RuntimeError("ollama down")
        assert llm_parse("fill it", self._scan(), ask) is None

    def test_garbage_output_is_none(self):
        assert llm_parse("fill it", self._scan(), lambda p: "no json here") is None


# --------------------------------------------------------------------------- #
# The skills                                                                   #
# --------------------------------------------------------------------------- #

class TestReadForm:
    def test_lists_fields_and_button(self):
        ctx, tree, _ = _ctx()
        res = asyncio.run(AXReadForm().execute({}, ctx))
        assert res.ok
        assert "First Name" in res.message and "Place order" in res.message
        assert res.data["fields"][0] == "First Name"

    def test_no_form_is_honest(self):
        tree = FormTree()
        tree.root_node.children = [AXNode("statictext", label="no fields here")]
        ctx, _, _ = _ctx(tree)
        res = asyncio.run(AXReadForm().execute({}, ctx))
        assert not res.ok and "don't see a form" in res.message


class TestFillForm:
    def test_fills_dictated_fields(self):
        ctx, tree, _ = _ctx()
        res = asyncio.run(AXFillForm().execute(
            {"raw": "fill this form: first name John, email john@smith.com, city Reno"},
            ctx))
        assert res.ok
        assert res.data["filled"] == ["First Name", "Email", "City"]
        assert res.data["unfilled"] == ["Last Name", "Phone",
                                        "Street Address", "Notes"]
        inserts = [d for kind, d in tree.log if kind == "insert"]
        assert any("John" in d for d in inserts)
        assert "Place order" not in [l for k, l in tree.log if k == "press"]

    def test_explicit_fields_win_over_raw(self):
        ctx, tree, _ = _ctx()
        res = asyncio.run(AXFillForm().execute(
            {"fields": [{"label": "Email", "value": "planner@parsed"}],
             "raw": "fill this form: email raw@parsed"}, ctx))
        assert res.ok and res.data["filled"] == ["Email"]
        got = {d.split(" ← ")[0]: d.split(" ← ")[1]
               for k, d in tree.log if k == "insert"}
        assert got["Email"] == "planner@parsed"

    def test_submit_button_only_when_asked(self):
        ctx, tree, _ = _ctx()
        res = asyncio.run(AXFillForm().execute(
            {"raw": "fill this form: city Reno", "submit": "Place order"}, ctx))
        assert res.ok
        assert ("press", "Place order") in tree.log
        assert "Place order" in res.message

    def test_no_match_reports_the_form(self):
        ctx, tree, _ = _ctx()
        res = asyncio.run(AXFillForm().execute(
            {"raw": "fill this form: weather sunny"}, ctx))
        assert not res.ok
        assert "First Name" in res.message and "Place order" in res.message
        assert tree.log == []

    def test_no_form_on_screen(self):
        tree = FormTree()
        tree.root_node.children = []
        ctx, _, _ = _ctx(tree)
        res = asyncio.run(AXFillForm().execute({"raw": "fill it"}, ctx))
        assert not res.ok and "frontmost" in res.message


class TestDictate:
    def test_types_into_focus(self):
        ctx, tree, bridge = _ctx()
        res = asyncio.run(AXDictate().execute({"text": "123 Main Street"}, ctx))
        assert res.ok and bridge.keystrokes == ["123 Main Street"]

    def test_empty_rejected(self):
        ctx, _, bridge = _ctx()
        res = asyncio.run(AXDictate().execute({"text": "   "}, ctx))
        assert not res.ok and bridge.keystrokes == []


# --------------------------------------------------------------------------- #
# Planner routing — the dictation entry point                                   #
# --------------------------------------------------------------------------- #

class TestRouting:
    def test_fill_form_routes(self, planner):
        plan = _plan(planner, "Fill this form: first name John, email john@smith.com")
        assert [a.skill for a in plan.actions] == ["ax.fill_form"]
        assert "john@smith.com" in plan.actions[0].args["raw"]   # case kept

    def test_fill_and_submit_is_two_actions(self, planner):
        plan = _plan(planner, "Fill this form: city Reno, and press submit")
        assert [a.skill for a in plan.actions] == ["ax.fill_form", "ax.click"]
        assert plan.actions[1].args == {"target": "submit"}
        assert plan.actions[1].risk == "confirm"

    def test_complete_the_form(self, planner):
        plan = _plan(planner, "Complete the signup form and press sign up")
        assert [a.skill for a in plan.actions] == ["ax.fill_form", "ax.click"]
        assert plan.actions[1].args["target"] == "sign up"

    def test_read_form_routes(self, planner):
        plan = _plan(planner, "What fields does this form have?")
        assert [a.skill for a in plan.actions] == ["ax.read_form"]

    def test_plain_dictation(self, planner):
        plan = _plan(planner, "type 123 Main Street")
        assert [a.skill for a in plan.actions] == ["ax.dictate"]
        assert plan.actions[0].args["text"] == "123 Main Street"

    def test_dictation_preserves_case(self, planner):
        plan = _plan(planner, "Type Hunter2!")
        assert plan.actions[0].args["text"] == "Hunter2"

    def test_type_into_named_field_keeps_case(self, planner):
        plan = _plan(planner, "type MyPass123 into the password field")
        assert [a.skill for a in plan.actions] == ["ax.type_into"]
        assert plan.actions[0].args["text"] == "MyPass123"
        assert plan.actions[0].args["target"] == "password field"

    def test_regular_commands_still_route(self, planner):
        assert _plan(planner, "open youtube").actions[0].skill == "browser.open_url"
        plan = _plan(planner, "open spotify and set volume to 30")
        assert [a.skill for a in plan.actions] == ["system.open_app",
                                                   "system.set_volume"]


# --------------------------------------------------------------------------- #
# Safety — filling is fast; pressing the send button asks first                #
# --------------------------------------------------------------------------- #

def test_gate_allows_fill_and_asks_before_submit(stack):
    from aura.planner import Action

    gate = stack.safety
    known = stack.registry.names()
    fill = Action("ax.fill_form",
                  {"raw": "fill this form: first name John, city Reno"}, "safe", "")
    assert gate.assess(fill, "fill this form: first name John, city Reno",
                       known).decision == "run"

    with_submit = Action("ax.fill_form",
                         {"raw": "fill this form: city Reno, then submit"}, "safe", "")
    assert gate.assess(with_submit, "fill this form: city Reno, then submit",
                       known).decision == "confirm"

    press = Action("ax.click", {"target": "submit"}, "confirm", "")
    assert gate.assess(press, "press submit", known).decision == "confirm"


# --------------------------------------------------------------------------- #
# The full dry-run story — the chip on the overview page                        #
# --------------------------------------------------------------------------- #

def test_demo_chip_command_end_to_end(stack):
    """'Fill the form' chip: parse the demo tree, type, report — one session."""
    orch = stack.build_orchestrator()

    async def run():
        await orch.start()
        try:
            await orch.submit_text("Fill this form: username demo, password demo123")
            for _ in range(100):
                if orch.state == "armed" and orch.session is None:
                    break
                await asyncio.sleep(0.02)
        finally:
            await orch.stop()

    asyncio.run(run())
    transcript = orch.memory.search("demo")
    assert transcript, "the session should have run to completion"
    # The demo tree's sign-in form must actually have been typed into.
    tree = stack.bridge.ax_tree()
    inserts = [detail for kind, detail in tree.log if kind == "insert"]
    assert "Username ← demo" in inserts, inserts
    assert "Password ← demo123" in inserts, inserts
    # …and no submit button was pressed (we only asked to fill).
    assert not [l for k, l in tree.log if k == "press"]
