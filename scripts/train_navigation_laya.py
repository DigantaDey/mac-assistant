#!/usr/bin/env python3
"""Train the Aura navigation checkpoint — a real Laya decision model.

Laya (Apache-2.0, Convai Innovations) is a non-autoregressive "System 1"
model: it answers *typed questions* (choice / score / noul) with calibrated
probabilities in a single forward pass, and it never generates text. Aura's
whole brain is built from exactly those answers:

    choice  "Which skill carries out this request?"   → voice navigation
    noul    "Does this action match the request?"     → the safety gate
    noul    "Is this action destructive?"             → the safety gate
    score   "What volume level does the user want?"   → scales

The official checkpoint on the Hugging Face hub is ~421M parameters and needs
a download. This script builds the *navigation checkpoint* instead: the same
architecture, the same runtime (`laya.Agent` / `laya.Router`), trained from
scratch on Aura's own domain — the skill catalog, the phrasings people
actually say, and the gate questions exactly as the engine asks them. It is a
few MB, runs in milliseconds on a CPU, and needs no network once built.

The output directory is a native Laya checkpoint:

    rl_agent_config.json   model.safetensors   tokenizer/   encoder/

so `RealLayaBackend` loads it through `Router(models={"english": dir})`
without a single line of special-casing — and `python -m aura laya-check`
proves it end-to-end.

Usage:
    python scripts/train_navigation_laya.py --out assets/models/aura-nav-laya
    python scripts/train_navigation_laya.py --out assets/models/aura-nav-laya --steps 600
    python scripts/train_navigation_laya.py --eval assets/models/aura-nav-laya
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

# --------------------------------------------------------------------------- #
# The domain: apps, sites, and the things people say                           #
# --------------------------------------------------------------------------- #

APPS = [
    "Safari", "Spotify", "Notes", "Mail", "Calendar", "Finder", "Terminal",
    "Messages", "Maps", "Photos", "FaceTime", "Music", "Settings",
    "System Settings", "TextEdit", "Preview", "Activity Monitor", "Xcode",
    "Slack", "Zoom", "Pages", "Numbers", "Keynote", "Reminders", "Clock",
    "Calculator", "Chess", "Podcasts", "TV", "News", "Stocks", "Books",
    "Voice Memos", "Stickies", "Font Book", "Disk Utility", "Time Machine",
    "Mission Control", "App Store", "Contacts", "Dictionary", "Weather",
]

SITES = [
    "youtube", "github", "gmail", "google", "maps", "calendar", "whatsapp",
    "reddit", "wikipedia", "twitter", "netflix", "amazon", "stackoverflow",
    "linkedin", "instagram", "twitch", "duckduckgo",
]

SEARCH_TOPICS = [
    "airport lounges", "best ramen near me", "weather tomorrow",
    "python decorators", "flight status BA 249", "how to tie a bowtie",
    "the nearest pharmacy", "italian restaurants open now",
    "how long to boil an egg", "distance to the moon",
    "recipe for banana bread", "today's cricket score", "electricity bill",
    "train times to Cambridge", "cheap hotels in Lisbon",
]

CLICK_TARGETS = [
    "the sign in button", "send", "submit", "the search field", "next",
    "the close button", "agree", "settings", "the mute button", "skip",
    "the play button", "cancel", "the menu", "notifications", "the bell icon",
]

FIELDS = ["the address field", "the email field", "the name field",
          "the password field", "the search box", "the comment box",
          "the phone number field", "the username field"]

DICTATIONS = [
    "running a bit late, sorry", "please call me back", "on my way",
    "the meeting moved to Thursday", "sounds great, count me in",
    "pick up some milk on the way home", "can we talk later",
]

CHIT_CHAT = [
    "hello there", "who are you", "tell me a joke", "what time is it",
    "are you listening", "good morning", "good night", "how are you today",
    "what is the meaning of life", "sing me a song", "what can you do",
    "tell me about yourself", "do you like music", "are you real",
    "what should I eat for dinner", "is it going to rain",
    "recommend a movie", "what's new", "bored", "thank you",
]

UNSUPPORTED = [
    "order me a pizza", "book a flight to Paris", "transfer money to Bob",
    "email my boss", "post this to twitter", "schedule a haircut",
    "turn on the kitchen lights", "set an alarm for 7 am",
    "call mom", "send a message to Alice", "buy concert tickets",
    "translate this to French", "draw me a picture", "play chess with me",
]

OPEN_TEMPLATES = [
    "open {x}", "launch {x}", "start {x}", "run {x}", "open the {x} app",
    "open up {x}", "bring up {x}", "fire up {x}", "pull up {x}",
    "can you open {x}", "please open {x}", "hey aura open {x}",
    "switch to {x}", "I need {x}", "get {x} open", "open {x} for me",
]

SITE_TEMPLATES = [
    "open {x}", "go to {x}", "open {x} dot com", "take me to {x}",
    "visit {x}", "pull up {x}", "open the {x} website", "browse to {x}",
    "hey aura open {x}", "can you go to {x}",
]

QUIT_TEMPLATES = [
    "quit {x}", "close {x}", "quit the {x} app", "force quit {x}",
    "kill {x}", "close {x} please", "exit {x}", "shut down {x}",
    "can you quit {x}", "hey aura close {x}",
]

SEARCH_TEMPLATES = [
    "search for {x}", "google {x}", "look up {x}", "search the web for {x}",
    "find {x}", "do a web search for {x}", "look {x} up", "search {x} online",
]

VOLUME_EXACT_TEMPLATES = [
    "set volume to {n}", "volume {n}", "change the volume to {n} percent",
    "turn the volume to {n}", "set the volume to {n} please",
    "I want the volume at {n}", "volume level {n}",
]

VOLUME_RELATIVE = {
    "max": ["max volume", "turn it all the way up", "full volume",
            "volume to the maximum", "blast it", "crank it up"],
    "half": ["half volume", "set the volume to half", "volume fifty percent",
             "halfway on the volume"],
    "low": ["turn it down", "lower the volume", "quieter please",
            "turn the volume down a bit", "a bit quieter", "keep it down",
            "too loud", "shh"],
    "high": ["turn it up", "louder", "louder please", "raise the volume",
             "I can't hear it", "turn the sound up a bit", "a little louder"],
    "mute": ["mute", "mute the sound", "silence", "mute it please",
             "quiet the sound", "turn the sound off"],
}

BRIGHTNESS_UP = ["brighter", "increase the brightness", "brighten the screen",
                 "the screen is too dark", "turn the brightness up"]
BRIGHTNESS_DOWN = ["dim the screen", "lower the brightness", "too bright",
                   "turn the brightness down", "dimmer please"]

DND = ["quiet the house", "do not disturb", "turn on focus", "focus mode",
       "turn on do not disturb", "silence notifications", "no notifications",
       "I'm in a meeting", "don't let anyone bother me", "quiet hours"]

TRASH = ["empty the trash", "empty trash", "clear the trash",
         "empty the bin", "clean out the trash"]
SLEEP = ["sleep", "put the mac to sleep", "put this machine to bed",
         "go to sleep", "send the mac to sleep"]
LOCK = ["lock the screen", "lock my mac", "lock it", "lock the computer",
        "screen lock please"]
RECORD = ["start recording", "record the screen", "start a screen recording",
          "begin recording my screen"]
REMEMBER = ["remember that {x}", "note that {x}", "keep in mind {x}"]
REMEMBER_FACTS = ["the wifi password is blue ocean 42",
                  "my flight is at six tomorrow",
                  "parking is on level three",
                  "the standup moved to Tuesdays",
                  "dad's birthday is March third"]

TABS_LIST = ["list my tabs", "read my open tabs", "what tabs are open",
             "show me my tabs"]
TAB_FOCUS = ["switch to the {x} tab", "go to the {x} tab", "focus the {x} tab"]
TAB_TITLES = ["github", "youtube", "gmail", "inbox", "news", "calendar"]

CLIP_READ = ["read the clipboard", "what's on the clipboard",
             "what is in my clipboard"]
CLIP_SET = ['copy "{x}" to the clipboard', "copy {x}"]
CLIP_TEXTS = ["hello world", "the wifi password", "my agenda today",
              "see you tomorrow"]

CLICK_TEMPLATES = ["click {x}", "press {x}", "tap {x}", "click on {x}",
                   "hit {x}", "press the {x}"]
TYPE_INTO_TEMPLATES = ["type {v} into {f}", "enter {v} in {f}",
                       "put {v} into {f}", "write {v} in {f}"]
TYPE_VALUES = ["hello there", "John Smith", "yes", "no thanks",
               "bob at example dot com", "123 Main Street", "see you at five"]
DICTATE_TEMPLATES = ["type {v}", "dictate {v}", "say {v}"]
READ_FORM = ["what fields are on this form", "list the form fields",
             "read the form", "what does this form ask"]
READ_SCREEN = ["what's on my screen", "read my screen", "what do you see",
               "describe what's on screen"]
FILL_FORM = ["fill this form with my details",
             "fill in the form: name {n}, email {e}",
             "complete the form with my information",
             "fill the fields with my details and submit"]

CHAIN_TEMPLATES = ["open {a} and set volume to {n}",
                   "mute and open {a}",
                   "open {a} and then open {b}"]


def _rng(seed: int) -> random.Random:
    return random.Random(seed)


# --------------------------------------------------------------------------- #
# Sample generation — exactly the shapes the runtime feeds Laya                #
# --------------------------------------------------------------------------- #


def routing_samples(seed: int = 7) -> list[tuple[str, str]]:
    """(transcript, skill label) pairs covering every catalog skill + none."""
    r = _rng(seed)
    out: list[tuple[str, str]] = []

    def add(templates: list[str], skill: str, xs: list[str], n: int,
            subs: dict | None = None) -> None:
        picks = [r.choice(xs) for _ in range(n)] if xs else [""] * n
        for x in picks:
            t = r.choice(templates)
            if subs:
                t = t.format(**{k: r.choice(v) for k, v in subs.items()})
            out.append((t.format(x=x) if "{x}" in t else t, skill))

    add(OPEN_TEMPLATES, "system.open_app", APPS, 260)
    add(SITE_TEMPLATES, "browser.open_url", SITES, 120)
    add(QUIT_TEMPLATES, "system.quit_app", APPS, 130)
    add(SEARCH_TEMPLATES, "browser.search", SEARCH_TOPICS, 130)
    add(VOLUME_EXACT_TEMPLATES, "system.set_volume", [], 90)
    for n in range(90):
        out.append((r.choice(VOLUME_EXACT_TEMPLATES).format(n=r.randint(0, 100)),
                    "system.set_volume"))
    for level, phrases in VOLUME_RELATIVE.items():
        skill = "system.mute" if level == "mute" else "system.set_volume"
        for _ in range(46):
            out.append((r.choice(phrases), skill))

    for phrase in BRIGHTNESS_UP:
        for _ in range(12):
            out.append((phrase, "system.brightness_up"))
    for phrase in BRIGHTNESS_DOWN:
        for _ in range(12):
            out.append((phrase, "system.brightness_down"))
    for _ in range(140):
        out.append((r.choice(DND), "system.toggle_dnd"))
    for _ in range(60):
        out.append((r.choice(TRASH), "system.empty_trash"))
    for _ in range(60):
        out.append((r.choice(SLEEP), "system.sleep"))
    for _ in range(60):
        out.append((r.choice(LOCK), "system.lock_screen"))
    for _ in range(60):
        out.append((r.choice(RECORD), "system.start_recording"))
    for _ in range(70):
        out.append((r.choice(REMEMBER).format(x=r.choice(REMEMBER_FACTS)),
                    "system.remember"))
    for _ in range(50):
        out.append((r.choice(TABS_LIST), "browser.list_tabs"))
    for _ in range(60):
        out.append((r.choice(TAB_FOCUS).format(x=r.choice(TAB_TITLES)),
                    "browser.focus_tab"))
    for _ in range(50):
        out.append((r.choice(CLIP_READ), "clipboard.get_text"))
    for _ in range(60):
        out.append((r.choice(CLIP_SET).format(x=r.choice(CLIP_TEXTS)),
                    "clipboard.set_text"))
    add(CLICK_TEMPLATES, "ax.click", CLICK_TARGETS, 110)
    for _ in range(110):
        out.append((r.choice(TYPE_INTO_TEMPLATES).format(
            v=r.choice(TYPE_VALUES), f=r.choice(FIELDS)), "ax.type_into"))
    for _ in range(80):
        out.append((r.choice(DICTATE_TEMPLATES).format(v=r.choice(DICTATIONS)),
                    "ax.dictate"))
    for _ in range(50):
        out.append((r.choice(READ_FORM), "ax.read_form"))
    for _ in range(60):
        out.append((r.choice(READ_SCREEN), "ax.read_screen"))
    for _ in range(60):
        out.append((r.choice(FILL_FORM), "ax.fill_form"))
    for _ in range(220):
        out.append((r.choice(CHIT_CHAT), "none"))
    for _ in range(160):
        out.append((r.choice(UNSUPPORTED), "none"))
    r.shuffle(out)
    return out


#: Gate labels by skill: is executing this, in itself, destructive/irreversible?
DESTRUCTIVE_SKILL_LABEL = {
    "system.empty_trash": 1, "system.sleep": 1, "system.quit_app": 1,
    "system.start_recording": 1, "system.toggle_dnd": 0, "system.mute": 0,
    "system.set_volume": 0, "system.open_app": 0, "browser.open_url": 0,
    "browser.search": 0, "browser.list_tabs": 0, "browser.focus_tab": 0,
    "clipboard.get_text": 0, "clipboard.set_text": 0, "ax.click": 0,
    "ax.type_into": 0, "ax.dictate": 0, "ax.fill_form": 0, "ax.read_form": 0,
    "ax.read_screen": 0, "system.remember": 0, "system.brightness_up": 0,
    "system.brightness_down": 0, "system.lock_screen": 0,
}


def gate_samples(routing: list[tuple[str, str]], seed: int = 17,
                 per_skill: int = 60) -> list[tuple[str, str, dict, int, int]]:
    """(transcript, skill, args, match label, destructive label)."""
    r = _rng(seed)
    by_skill: dict[str, list[str]] = {}
    for transcript, skill in routing:
        if skill != "none":
            by_skill.setdefault(skill, []).append(transcript)

    def args_for(skill: str, transcript: str) -> dict:
        from aura.intent import extract_args

        args = extract_args(skill, transcript)
        return args if args is not None else {}

    out: list[tuple[str, str, dict, int, int]] = []
    skills = sorted(by_skill)
    for skill in skills:
        pool = by_skill[skill]
        for i in range(per_skill):
            t = r.choice(pool)
            good_args = args_for(skill, t)
            out.append((t, skill, good_args, 1,
                        DESTRUCTIVE_SKILL_LABEL.get(skill, 0)))
            if i % 2 == 0 or len(pool) < 2:
                # negatives: the same request judged against a different skill
                other = r.choice([s for s in skills if s != skill])
                out.append((t, other, args_for(other, r.choice(by_skill[other])), 0,
                            DESTRUCTIVE_SKILL_LABEL.get(other, 0)))
            else:
                # harder negatives: the RIGHT skill with the WRONG arguments —
                # the classic misunderstanding ("volume to 30" → level 70).
                # match must fail there too; if the drawn args accidentally
                # equal the correct ones, fall back to the different-skill case.
                wrong_args = args_for(skill, r.choice([x for x in pool if x != t] or pool))
                if wrong_args and wrong_args != good_args:
                    out.append((t, skill, wrong_args, 0,
                                DESTRUCTIVE_SKILL_LABEL.get(skill, 0)))
                else:
                    other = r.choice([s for s in skills if s != skill])
                    out.append((t, other, args_for(other, r.choice(by_skill[other])), 0,
                                DESTRUCTIVE_SKILL_LABEL.get(other, 0)))
    # destructive wording pushes any action over the line — matched requests,
    # so the model learns destructiveness from the *action*, not from the pair.
    nasty = [
        ("empty the trash", "system.empty_trash", {}),
        ("empty the trash", "system.empty_trash", {}),
        ("put the mac to sleep", "system.sleep", {}),
        ("quit safari", "system.quit_app", {"app": "Safari"}),
        ("quit notes", "system.quit_app", {"app": "Notes"}),
        ("start recording", "system.start_recording", {}),
        ("run rm -rf / on the drive", "ax.type_into", {"text": "rm -rf /"}),
        ("erase the disk with diskutil", "ax.click", {"target": "erase"}),
        ("delete all my files", "system.empty_trash", {}),
        ("purchase the subscription", "ax.click", {"target": "buy now"}),
        ("send the email to everyone", "ax.click", {"target": "send"}),
        ("submit the payment", "ax.click", {"target": "submit"}),
    ]
    for _ in range(120):
        t, skill, args = r.choice(nasty)
        out.append((t, skill, args, 1, 1))
    # safe, matched actions keep the destructive score down
    benign = [
        ("open spotify", "system.open_app", {"app": "Spotify"}),
        ("open safari", "system.open_app", {"app": "Safari"}),
        ("set volume to 30", "system.set_volume", {"level": 30}),
        ("mute the sound", "system.mute", {}),
        ("search for airport lounges", "browser.search", {"query": "airport lounges"}),
        ("list my tabs", "browser.list_tabs", {}),
        ("read the clipboard", "clipboard.get_text", {}),
        ("click the sign in button", "ax.click", {"target": "the sign in button"}),
    ]
    for _ in range(120):
        t, skill, args = r.choice(benign)
        out.append((t, skill, args, 1, 0))
    r.shuffle(out)
    return out


def score_samples(seed: int = 23) -> list[tuple[str, int]]:
    """(transcript, target index on the 0,10,…,100 scale)."""
    r = _rng(seed)
    out: list[tuple[str, int]] = []
    for _ in range(200):
        level = r.randint(0, 100)
        t = r.choice(VOLUME_EXACT_TEMPLATES).format(n=level)
        out.append((t, round(level / 10)))
    for idx, phrases in ((10, VOLUME_RELATIVE["max"]), (5, VOLUME_RELATIVE["half"]),
                         (2, VOLUME_RELATIVE["low"]), (8, VOLUME_RELATIVE["high"]),
                         (0, VOLUME_RELATIVE["mute"])):
        for _ in range(40):
            out.append((r.choice(phrases), idx))
    r.shuffle(out)
    return out


# --------------------------------------------------------------------------- #
# The runtime question shapes — must match aura/laya.py and aura/intent.py     #
# --------------------------------------------------------------------------- #


def runtime_pieces():
    from aura.intent import NONE, NONE_TEXT, ROUTE_INSTRUCTIONS, coerce_specs, shortlist
    from aura.laya import GATE_QUESTIONS
    from aura.skills import DryRunBridge, build_default_registry

    registry = build_default_registry(DryRunBridge())
    specs = coerce_specs(registry.specs())
    return registry, specs, shortlist, NONE, NONE_TEXT, ROUTE_INSTRUCTIONS, GATE_QUESTIONS


def route_options_for(transcript: str, specs, shortlist, none, none_text,
                      shortlist_size: int = 12) -> dict[str, str]:
    candidates = shortlist(transcript, specs, size=shortlist_size)
    options = {spec.name: spec.option_text() for spec in candidates}
    options[none] = none_text
    return options


def gate_state(transcript: str, skill: str, args: dict, why: str = "") -> str:
    return json.dumps({"request": transcript,
                       "action": {"skill": skill, "args": args, "rationale": why}})


# --------------------------------------------------------------------------- #
# Checkpoint scaffolding — a native Laya checkpoint directory                  #
# --------------------------------------------------------------------------- #

ENCODER_KWARGS = {
    "vocab_size": 4096, "hidden_size": 160, "num_hidden_layers": 2,
    "num_attention_heads": 4, "intermediate_size": 640,
    "max_position_embeddings": 640, "type_vocab_size": 2,
    "pad_token_id": 0, "classifier_dropout": 0.1, "hidden_dropout_prob": 0.1,
    "attention_probs_dropout_prob": 0.1,
}
AGENT_CFG_EXTRA = {"max_len": 512, "head_max_len": 448, "head_layers": 2,
                   "temperature": [1.0, 1.0, 1.0], "act_costs": {}}


def build_tokenizer(out: Path, texts: list[str], vocab_size: int = 4096) -> None:
    from tokenizers import Tokenizer, trainers
    from tokenizers.models import WordPiece
    from tokenizers.normalizers import NFKC, Lowercase, Sequence
    from tokenizers.pre_tokenizers import Whitespace

    tok = Tokenizer(WordPiece(unk_token="[UNK]"))
    tok.normalizer = Sequence([NFKC(), Lowercase()])
    tok.pre_tokenizer = Whitespace()
    trainer = trainers.WordPieceTrainer(
        vocab_size=vocab_size,
        special_tokens=["[PAD]", "[UNK]", "[CLS]", "[SEP]", "[MASK]"],
        min_frequency=2,
    )
    tok.train_from_iterator(texts, trainer)
    tok.save(str(out / "tokenizer" / "tokenizer.json"))

    from transformers import PreTrainedTokenizerFast

    fast = PreTrainedTokenizerFast(
        tokenizer_file=str(out / "tokenizer" / "tokenizer.json"),
        unk_token="[UNK]", pad_token="[PAD]", cls_token="[CLS]",
        sep_token="[SEP]", mask_token="[MASK]",
    )
    fast.save_pretrained(out / "tokenizer")


def build_encoder_config(out: Path) -> None:
    from transformers import BertConfig

    cfg = BertConfig(**ENCODER_KWARGS)
    cfg.save_pretrained(out / "encoder")


def write_agent_config(out: Path) -> None:
    cfg = {"encoder": "aura-nav-laya-encoder", **AGENT_CFG_EXTRA}
    (out / "rl_agent_config.json").write_text(json.dumps(cfg, indent=2))


def scaffold(out: Path, texts: list[str]) -> None:
    out.mkdir(parents=True, exist_ok=True)
    (out / "tokenizer").mkdir(exist_ok=True)
    build_tokenizer(out, texts)
    build_encoder_config(out)
    write_agent_config(out)


def init_weights(out: Path) -> None:
    """Initialise every parameter and save it as the checkpoint.

    `build_model(..., pretrained=False)` constructs the whole model with
    initialisation skipped (meta-device head, `_no_init_weights()` encoder) —
    by design, a checkpoint is loaded straight over it. For *training* we must
    initialise it ourselves, and thoroughly: transformers removed
    `reset_parameters` from `nn.MultiheadAttention`, so a generic
    "call reset_parameters where present" pass silently leaves garbage in the
    attention projections — which reads as NaN on the first forward.
    """
    import torch
    from laya.common import build_model
    from safetensors.torch import save_file

    cfg = json.loads((out / "rl_agent_config.json").read_text())
    model = build_model(cfg, encoder_dir=str(out / "encoder"), pretrained=False)

    # The encoder is a plain HF model: its own `_init_weights` covers every
    # submodule it contains (embeddings, Linear, LayerNorm).
    init_fn = getattr(model.encoder, "init_weights", None)
    if callable(init_fn):
        init_fn()
    else:                                  # pragma: no cover - old transformers
        model.encoder.apply(model.encoder._init_weights)

    def _init_module(module: torch.nn.Module) -> None:
        if isinstance(module, torch.nn.Linear):
            torch.nn.init.xavier_uniform_(module.weight)
            if module.bias is not None:
                torch.nn.init.zeros_(module.bias)
        elif isinstance(module, torch.nn.LayerNorm):
            torch.nn.init.ones_(module.weight)
            torch.nn.init.zeros_(module.bias)
        elif isinstance(module, torch.nn.Embedding):
            torch.nn.init.normal_(module.weight, mean=0.0, std=0.02)
        elif isinstance(module, torch.nn.MultiheadAttention):
            # Includes laya's _DynamicMultiheadAttention subclass.
            if module.in_proj_weight is not None:
                torch.nn.init.xavier_uniform_(module.in_proj_weight)
            for part in ("q_proj_weight", "k_proj_weight", "v_proj_weight"):
                w = getattr(module, part, None)
                if w is not None:
                    torch.nn.init.xavier_uniform_(w)
            if module.in_proj_bias is not None:
                torch.nn.init.zeros_(module.in_proj_bias)
            torch.nn.init.xavier_uniform_(module.out_proj.weight)
            if module.out_proj.bias is not None:
                torch.nn.init.zeros_(module.out_proj.bias)

    for part in (model.head, model.scorer, model.act_head):
        if part is not None:
            part.apply(_init_module)
    with torch.no_grad():
        model.type_emb.weight.normal_(0, 0.02)
        model.temperature.fill_(1.0)

    bad = [name for name, p in model.state_dict().items()
           if torch.is_floating_point(p) and not torch.isfinite(p).all()]
    if bad:
        raise RuntimeError(f"initialisation left non-finite parameters: {bad[:5]}")
    save_file({k: v.contiguous() for k, v in model.state_dict().items()},
              str(out / "model.safetensors"))


# --------------------------------------------------------------------------- #
# Training                                                                     #
# --------------------------------------------------------------------------- #


def make_agent(out: Path):
    import torch
    from laya import Agent

    agent = Agent(str(out), device="cpu")
    bad = [name for name, p in agent.model.state_dict().items()
           if torch.is_floating_point(p) and not torch.isfinite(p).all()]
    if bad:
        raise RuntimeError(f"checkpoint {out} loaded with non-finite parameters "
                           f"(e.g. {bad[:3]}) — re-run the trainer")
    return agent


def _encode_batch(agent, samples):
    """Encode (state, question, target) samples through the Agent's own path."""
    items_all = []
    targets = []
    kinds = []
    for state, qdef, target in samples:
        qid = "q"
        agent._check_question(qid, qdef)
        internal = {qid: agent._to_internal(qdef)}
        items = agent._encode_state(state, [qid], internal)
        items_all.append(items)
        targets.append(target)
        kinds.append(qdef["type"])
    from laya.agent import collate_items

    batch = collate_items(items_all, agent.tok.pad_token_id)
    return batch, items_all, targets, kinds


def load_feedback_gates(path: Path, base_repeat: int = 4) -> list[tuple[str, str, dict, int, int]]:
    """Real user verdicts from the experience buffer (nightly_laya.py).

    Lines are in the `ExampleBuffer.export_jsonl` format:

        {"state": "{\"request\": …, \"action\": {\"skill\": …, \"args\": …}}",
         "questions": [{"question": …, "answer": {"no": …, "yes": …}}, …],
         "weight": …}

    Every verdict is repeated so a small buffer still moves the weights
    (weighted: a correction counts more than a plain auto-run), and the
    destructive label is clamped to *at least* the skill's default — user
    feedback may teach the gate that a benign skill was misused, never that a
    destructive one suddenly isn't.
    """
    from aura.laya import Q_DESTRUCTIVE, Q_MATCH

    out: list[tuple[str, str, dict, int, int]] = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
            state = json.loads(item["state"])
            transcript = str(state["request"])
            action = state.get("action") or {}
            skill = str(action.get("skill") or "")
            args = dict(action.get("args") or {})
            answers = {q["question"]: q["answer"] for q in item.get("questions", [])}
            match = 1 if answers.get(Q_MATCH, {}).get("yes", 0.0) >= 0.5 else 0
            dest = 1 if answers.get(Q_DESTRUCTIVE, {}).get("yes", 0.0) >= 0.5 else 0
            default_dest = DESTRUCTIVE_SKILL_LABEL.get(skill, 0)
            dest = max(dest, default_dest)
            weight = float(item.get("weight", 1))
        except (ValueError, KeyError, TypeError):
            continue
        repeats = max(1, round(base_repeat * weight))
        out.extend((transcript, skill, args, match, dest) for _ in range(repeats))
    return out


def train(out: Path, steps: int, batch_size: int, lr: float, seed: int,
          feedback_jsonl: Path | None = None) -> dict:
    import torch

    torch.manual_seed(seed)
    specs_pieces = runtime_pieces()
    _, specs, shortlist, none, none_text, route_instructions, gate_questions = specs_pieces

    routing = routing_samples(seed)
    gates = gate_samples(routing, seed + 10)
    if feedback_jsonl:
        gates = gates + load_feedback_gates(feedback_jsonl)
    scores = score_samples(seed + 16)

    # Hold out a slice for honest evaluation.
    r = _rng(seed + 99)
    r.shuffle(routing)
    n_hold = max(64, len(routing) // 8)
    holdout_routing = routing[:n_hold]
    train_routing = routing[n_hold:]
    holdout_gates = gates[: max(32, len(gates) // 8)]
    train_gates = gates[len(holdout_gates):]
    holdout_scores = scores[: max(32, len(scores) // 8)]
    train_scores = scores[len(holdout_scores):]

    print(f"dataset: routing={len(train_routing)}+{len(holdout_routing)} "
          f"gate={len(train_gates)}+{len(holdout_gates)} "
          f"score={len(train_scores)}+{len(holdout_scores)}")

    agent = make_agent(out)
    model = agent.model
    model.train()
    opt = torch.optim.AdamW(model.parameters(), lr=lr, weight_decay=0.01)
    sched = torch.optim.lr_scheduler.CosineAnnealingLR(opt, T_max=max(1, steps))


    ce = torch.nn.CrossEntropyLoss()

    def sample_batch() -> list[tuple]:
        batch = []
        for _ in range(batch_size):
            # Gate-heavy on purpose: matching and destructiveness are the
            # safety contract, and they are the hardest questions here — the
            # model must compare the request against the action, not just
            # classify the request.
            kind = r.choice(("route", "gate", "gate", "gate", "gate", "score"))
            if kind == "route":
                transcript, label = r.choice(train_routing)
                options = route_options_for(transcript, specs, shortlist, none,
                                            none_text)
                if label not in options:
                    # The shortlist is retrieval; the right skill must be in it
                    # for the model to ever pick it — exactly the runtime story.
                    spec = next(s for s in specs if s.name == label) \
                        if label != none else None
                    if spec is not None:
                        options[spec.name] = spec.option_text()
                qdef = {"type": "choice", "instructions": route_instructions,
                        "criteria": options}
                target = list(options.keys()).index(label)
                batch.append(({"request": transcript}, qdef, ("choice", target)))
            elif kind == "gate":
                transcript, skill, args, match, destr = r.choice(train_gates)
                # match is the undertrained half of the gate — it needs the
                # comparison the question asks for, so it gets 2× the reps
                qid = r.choice(("match", "match", "destructive"))
                qdef = dict(gate_questions[qid])
                target = match if qid == "match" else destr
                batch.append((gate_state(transcript, skill, args), qdef,
                              ("noul", target)))
            else:
                transcript, idx = r.choice(train_scores)
                qdef = {"type": "score",
                        "instructions": "What output volume level does the user want?",
                        "criteria": [str(n) for n in range(0, 101, 10)]}
                batch.append(({"request": transcript}, qdef, ("score", idx)))
        return batch

    running = {"loss": 0.0, "n": 0}
    t_start = time.time()
    for step in range(1, steps + 1):
        samples = sample_batch()
        batch, items_all, targets, kinds = _encode_batch(agent, samples)
        logits, act = model(batch["input_ids"], batch["attention_mask"],
                            batch["marker_pos"], batch["marker_mask"],
                            batch["qtype"])
        loss = torch.zeros((), device=logits.device)
        from laya.agent import _option_logits

        rows = _option_logits(logits, [items[0] for items in items_all], 0)
        for j, (kind, target) in enumerate(targets):
            row = rows[j]
            if kind in ("choice", "noul"):
                loss = loss + ce(row[None, : len(row)], torch.tensor([target]))
            else:  # score: soft target over the ordered scale
                k = len(row)
                dist = torch.zeros(k)
                dist[target] = 0.7
                for nb in (target - 1, target + 1):
                    if 0 <= nb < k:
                        dist[nb] = 0.15
                loss = loss + torch.nn.functional.kl_div(
                    torch.log_softmax(row, dim=-1), dist, reduction="sum")
        loss = loss / len(targets)
        opt.zero_grad()
        loss.backward()
        torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
        opt.step()
        sched.step()
        running["loss"] += loss.detach().item() * len(targets)
        running["n"] += len(targets)
        if step % 25 == 0 or step == steps:
            avg = running["loss"] / max(1, running["n"])
            elapsed = time.time() - t_start
            print(f"  step {step:4d}/{steps}  loss={avg:.4f}  ({elapsed:.0f}s)")
            running = {"loss": 0.0, "n": 0}

    metrics = evaluate(agent, holdout_routing, holdout_gates, holdout_scores,
                       specs, shortlist, none, none_text, route_instructions,
                       gate_questions)
    from safetensors.torch import save_file

    model.eval()
    save_file({k: v.contiguous() for k, v in model.state_dict().items()},
              str(out / "model.safetensors"))
    (out / "training_report.json").write_text(json.dumps({
        "steps": steps, "batch_size": batch_size, "lr": lr, "seed": seed,
        "trained_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        **metrics,
    }, indent=2))
    (out / "model_card.md").write_text(MODEL_CARD.format(**metrics))
    return metrics


MODEL_CARD = """# Aura navigation Laya checkpoint

A Laya (Apache-2.0, Convai Innovations) decision checkpoint trained for
Aura's keyboardless voice navigation: routing spoken requests to the skill
catalog (`choice`), judging match/destructive for the safety gate (`noul`),
and placing volume requests on a scale (`score`). Trained from scratch with
`scripts/train_navigation_laya.py` on generated navigation phrasings.

Hold-out metrics: route accuracy {route_acc:.1%} · gate match accuracy
{match_acc:.1%} · gate destructive accuracy {destr_acc:.1%} · score
mean absolute error {score_mae:.2f} levels.

Loads through the standard `laya.Router(models={{"english": <this dir>}})`.
"""


def evaluate(agent, holdout_routing, holdout_gates, holdout_scores, specs,
             shortlist, none, none_text, route_instructions,
             gate_questions) -> dict:
    import torch

    model = agent.model
    model.eval()
    from laya.agent import _option_logits

    def forward_choice(state, qdef) -> tuple[int, float]:
        batch, items_all, _, _ = _encode_batch(agent, [(state, qdef, ("x", 0))])
        with torch.no_grad():
            logits, _ = model(batch["input_ids"], batch["attention_mask"],
                              batch["marker_pos"], batch["marker_mask"],
                              batch["qtype"])
        row = _option_logits(logits, [items_all[0][0]], 0)[0]
        p = torch.softmax(row, -1)
        return int(p.argmax()), float(p.max())

    hits = 0
    for transcript, label in holdout_routing:
        options = route_options_for(transcript, specs, shortlist, none, none_text)
        if label not in options and label != none:
            spec = next(s for s in specs if s.name == label)
            options[spec.name] = spec.option_text()
        qdef = {"type": "choice", "instructions": route_instructions,
                "criteria": options}
        idx, _ = forward_choice({"request": transcript}, qdef)
        hits += list(options.keys())[idx] == label
    route_acc = hits / max(1, len(holdout_routing))

    m_hits = d_hits = 0
    for transcript, skill, args, match, destr in holdout_gates:
        qdef = dict(gate_questions["match"])
        idx, p = forward_choice(gate_state(transcript, skill, args), qdef)
        m_hits += idx == match
        qdef = dict(gate_questions["destructive"])
        idx, p = forward_choice(gate_state(transcript, skill, args), qdef)
        d_hits += idx == destr
    match_acc = m_hits / max(1, len(holdout_gates))
    destr_acc = d_hits / max(1, len(holdout_gates))

    err = 0.0
    for transcript, idx in holdout_scores:
        qdef = {"type": "score",
                "instructions": "What output volume level does the user want?",
                "criteria": [str(n) for n in range(0, 101, 10)]}
        batch, items_all, _, _ = _encode_batch(agent, [({"request": transcript},
                                                        qdef, ("x", 0))])
        with torch.no_grad():
            logits, _ = model(batch["input_ids"], batch["attention_mask"],
                              batch["marker_pos"], batch["marker_mask"],
                              batch["qtype"])
        row = _option_logits(logits, [items_all[0][0]], 0)[0]
        p = torch.softmax(row, -1)
        exp = float((torch.arange(len(p)).float() * p).sum())
        err += abs(exp - idx)
    score_mae = err / max(1, len(holdout_scores))

    print(f"eval: route={route_acc:.1%} match={match_acc:.1%} "
          f"destructive={destr_acc:.1%} score_mae={score_mae:.2f}")
    return {"route_acc": route_acc, "match_acc": match_acc,
            "destr_acc": destr_acc, "score_mae": score_mae}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=str(REPO_ROOT / "assets" / "models" / "aura-nav-laya"))
    ap.add_argument("--steps", type=int, default=500)
    ap.add_argument("--batch-size", type=int, default=8)
    ap.add_argument("--lr", type=float, default=3e-4)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--eval", metavar="DIR", default="",
                    help="evaluate an existing checkpoint and exit")
    ap.add_argument("--feedback-jsonl", metavar="PATH", default="",
                    help="JSONL of real user verdicts to fold into the gate "
                         "(see scripts/nightly_laya.py)")
    args = ap.parse_args()
    feedback = Path(args.feedback_jsonl).expanduser() if args.feedback_jsonl else None

    out = Path(args.out).expanduser()
    if args.eval:
        _, specs, shortlist, none, none_text, route_instructions, gate_questions \
            = runtime_pieces()
        agent = make_agent(Path(args.eval).expanduser())
        metrics = evaluate(agent, routing_samples(args.seed),
                           gate_samples(routing_samples(args.seed), args.seed + 10),
                           score_samples(args.seed + 16), specs, shortlist, none,
                           none_text, route_instructions, gate_questions)
        print(json.dumps(metrics, indent=2))
        return 0

    print(f"building checkpoint scaffolding at {out} …")
    texts = [t for t, _ in routing_samples(args.seed)]
    texts += [json.dumps({"request": t, "action": {"skill": s, "args": {}}})
              for t, s, _, _, _ in
              gate_samples(routing_samples(args.seed), args.seed + 10)]
    texts += [t for t, _ in score_samples(args.seed + 16)]
    _, specs, *_ = runtime_pieces()
    texts += [s.option_text() for s in specs]
    scaffold(out, texts)
    print("initialising weights …")
    init_weights(out)
    if feedback is not None:
        fb = load_feedback_gates(feedback)
        texts += [json.dumps({"request": t, "action": {"skill": s, "args": a}})
                  for t, s, a, _, _ in fb]
        print(f"folding in {len(fb)} feedback gate samples from {feedback}")
    print(f"training for {args.steps} steps …")
    metrics = train(out, args.steps, args.batch_size, args.lr, args.seed,
                    feedback_jsonl=feedback)
    ok = (metrics["route_acc"] >= 0.85 and metrics["match_acc"] >= 0.85
          and metrics["destr_acc"] >= 0.85)
    print(f"checkpoint at {out} — {'PASS' if ok else 'BELOW BAR'}")
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
