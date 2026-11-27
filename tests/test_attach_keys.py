"""Coming back from an attached agent, and seeing what an agent wrote without attaching.

Found on the first real use: Ctrl+] cannot be typed on a German keyboard (`]` is AltGr+9),
nothing on the agent's screen said how to return, and peek showed only Claude's empty input box."""

import sys

from ccdash.attach import DETACH_KEY, DetachScanner, detach_hint, detach_key_name
from ccdash.runner import strip_prompt_box
from ccdash.workspace import AgentView

from test_lookfeel import SIZE, frozen_app, ticks, view

F12 = "\x1b[24~"


def test_detach_on_f12_and_on_the_detach_character():
    s = DetachScanner()
    assert s.feed("hello") == ("hello", False)
    assert s.feed("ab" + DETACH_KEY + "cd") == ("ab", True), "what was typed before it still goes to the agent"
    assert DetachScanner().feed("x" + F12 + "y") == ("x", True)
    assert DetachScanner().feed("\x1b[24;5~") == ("", True), "with a modifier too"


def test_f12_split_over_two_reads_and_keys_that_only_look_like_it():
    s = DetachScanner()
    assert s.feed("a\x1b[2") == ("a", False), "could be the start of F12: held back"
    assert s.feed("4~") == ("", True)
    s = DetachScanner()
    assert s.feed("\x1b") == ("", False) and s.flush() == "\x1b", "a lone Esc is passed on when nothing follows"
    assert s.flush() == ""
    s = DetachScanner()
    assert s.feed("\x1b[") == ("", False) and s.feed("A") == ("\x1b[A", False), "arrow up, in two reads"
    assert DetachScanner().feed("\x1b[23~") == ("\x1b[23~", False), "F11 is not F12"
    assert DetachScanner().feed("\x1b[2~") == ("\x1b[2~", False), "nor is Insert"


def test_detach_hint_names_a_key_that_exists_on_this_keyboard():
    name = detach_key_name()
    assert name.startswith("Ctrl+") and len(name) > len("Ctrl+")
    assert detach_hint() == f"F12 or {name}"
    if sys.platform == "win32":
        import ctypes

        layout = ctypes.create_unicode_buffer(16)
        ctypes.WinDLL("user32").GetKeyboardLayoutNameW(layout)
        if layout.value.endswith("0407"):  # German: ] needs AltGr, the character is on the + key
            assert name == "Ctrl++"
        elif layout.value.endswith("0409"):  # US
            assert name == "Ctrl+]"


CLAUDE_SCREEN = """● Almost there: PR #377 got merged in the meantime.

  Kept: not merged
  - firmware-brownout (1), dev+build_test (2)

✽ Photosynthesizing… (2m 53s · ↓ 12.0k tokens)
  ⎿  Tip: Control this session from the Claude mobile app

───────────────────────────────────────────────────────────────────────────────────────── lead ─
❯
────────────────────────────────────────────────────────────────────────────────────────────────
  ⏵⏵ auto mode on (shift+tab to cycle) · esc to interrupt""".splitlines()


def test_peek_leaves_out_claudes_input_box():
    out = strip_prompt_box(CLAUDE_SCREEN)
    assert out[-1].startswith("  ⎿  Tip"), "what the agent wrote, down to its last line"
    assert not any(line.startswith("❯") or "auto mode on" in line for line in out)
    assert "  Kept: not merged" in out
    dialog = ["Do you want to make this edit to settings.py?", "❯ 1. Yes", "  2. No", "", "Esc to cancel"]
    assert strip_prompt_box(dialog) == dialog, "a permission dialog is not an input box: it stays"
    assert strip_prompt_box([]) == [] and strip_prompt_box(["fake-claude a", "> task"]) == ["fake-claude a", "> task"]
    typed = CLAUDE_SCREEN[:-3] + ["❯ half a sentence I am typi", "  ng over two lines", CLAUDE_SCREEN[-2], CLAUDE_SCREEN[-1]]
    assert strip_prompt_box(typed)[-1].startswith("  ⎿  Tip"), "also while something is typed into the box"


async def test_details_show_what_the_agent_said_last(repo):
    from ccdash.activity import AgentActivity

    lead = AgentView(name="lead", role="lead", status="idle", lifecycle="running", alive=True)
    app = frozen_app(repo, [lead, view("csv-export", "finished")], no_anim=True)
    answer = ("I started two agents. " + "csv-export adds the CSV export to the reports page and owns src/reports. " * 3 +
              "\n\n| agent | does |\n| csv-export | the export |")
    app.dash.activity.agents["lead"] = AgentActivity(name="lead", status="idle", last_message=answer)
    app.dash.activity.agents["csv-export"] = AgentActivity(name="csv-export", status="finished",
                                                           last_message="DONE: the export works and is tested")
    async with app.run_test(size=SIZE) as pilot:
        await ticks(pilot, 2)
        text = app.details_text()
        assert "said last" in text and "I started two agents." in text and "| csv-export | the export |" in text
        answer_lines = text.split("said last ──\n", 1)[1].splitlines()
        assert max(len(line) for line in answer_lines) <= app.query_one("#details").size.width, "wrapped to fit"
        assert max(len(line) for line in text.splitlines()) <= app.query_one("#details").size.width, "nothing is cut"
        app.select("csv-export")
        await pilot.pause()
        assert "DONE: the export works and is tested" in app.details_text(), "for every agent, not only the lead"


async def test_first_attach_says_how_to_come_back(repo):
    """The agent's screen fills the terminal and shows nothing of the dashboard: say it before."""
    attached = []
    app = frozen_app(repo, [view("csv-export", "working")], no_anim=True,
                     attach_fn=lambda name: attached.append(name) or 0)
    app._attach_explained = False  # as when attach is real
    async with app.run_test(size=SIZE) as pilot:
        await ticks(pilot, 2)
        await pilot.press("a")
        await pilot.pause()
        assert app.screen.id == "attach" and attached == []
        note = app.screen.query_one("#note").render().plain
        assert "comes back" in app.screen.query_one("#choices").render().plain
        assert app.detach_hint() in note and "keeps running" in note
        await pilot.press("escape")
        await pilot.pause()
        assert attached == [] and app.screen.id != "attach", "esc stays in the dashboard"
        await pilot.press("a")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert attached == ["csv-export"]
        await pilot.press("a")
        await pilot.pause()
        assert attached == ["csv-export", "csv-export"], "explained once, then a attaches at once"
