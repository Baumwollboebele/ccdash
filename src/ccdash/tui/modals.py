"""Modal dialogs."""

from __future__ import annotations

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, Label, Select, Static

from ..config import PERMISSION_MODES
from ..naming import is_valid_name, slugify, unique_name
from .theme import ACCENT


class NewAgentModal(ModalScreen[dict | None]):
    BINDINGS = [Binding("escape", "cancel", "cancel")]

    def __init__(self, taken: set[str], default_branch: str, model: str | None, permission_mode: str | None):
        super().__init__(id="new-agent")
        self.taken = taken
        self.default_branch = default_branch
        self.model = model
        self.permission_mode = permission_mode
        self._name_edited = False

    def compose(self) -> ComposeResult:
        with Vertical(classes="modal") as box:
            box.border_title = "New agent"
            yield Label("Task")
            yield Input(placeholder="What should the agent do?", id="task")
            yield Label("Name")
            yield Input(placeholder="derived from the task", id="name")
            yield Label("Base")
            yield Select([(f"{self.default_branch} (default branch)", "default"), ("HEAD of the main checkout", "HEAD")],
                         value="default", allow_blank=False, id="base")
            yield Label("Model")
            yield Input(value=self.model or "", placeholder="your default model", id="model")
            yield Label("Permission mode")
            modes = [("your default mode", "")] + [(m, m) for m in PERMISSION_MODES if m != "manual"]
            yield Select(modes, value=self.permission_mode or "", allow_blank=False, id="mode")
            yield Static("", id="error", classes="warn")
            yield Static(Text.assemble(("enter", ACCENT), " start  ", ("esc", ACCENT), " cancel"), classes="hint")

    def on_mount(self) -> None:
        self.query_one("#task", Input).focus()

    @on(Input.Changed, "#task")
    def _task_changed(self, event: Input.Changed) -> None:
        if not self._name_edited:
            name = self.query_one("#name", Input)
            with name.prevent(Input.Changed):
                name.value = unique_name(slugify(event.value), self.taken) if event.value.strip() else ""

    @on(Input.Changed, "#name")
    def _name_changed(self, event: Input.Changed) -> None:
        self._name_edited = True

    @on(Input.Submitted)
    def _submit(self, event: Input.Submitted) -> None:
        task = self.query_one("#task", Input).value.strip()
        name = self.query_one("#name", Input).value.strip()
        err = self.query_one("#error", Static)
        if not task:
            err.update("A task is required.")
            self.query_one("#task", Input).focus()
            return
        if not is_valid_name(name):
            err.update("Name: lowercase letters, digits and '-', at most 30 characters.")
            return
        if name in self.taken:
            err.update(f"{name} is already used by an agent, worktree or branch.")
            return
        base = self.query_one("#base", Select).value
        self.dismiss({
            "task": task,
            "name": name,
            "base": "HEAD" if base == "HEAD" else None,
            "model": self.query_one("#model", Input).value.strip() or None,
            "permission_mode": self.query_one("#mode", Select).value or None,
        })

    def action_cancel(self) -> None:
        self.dismiss(None)


class ConfirmDeleteModal(ModalScreen[str | None]):
    """Clean and merged: a simple y. Unmerged or uncommitted work: type the agent name."""

    BINDINGS = [Binding("escape", "cancel", "cancel"), Binding("n", "cancel", "no", show=False)]

    def __init__(self, check):
        super().__init__(id="confirm-delete")
        self.check = check

    def compose(self) -> ComposeResult:
        c = self.check
        with Vertical(classes="modal") as box:
            box.border_title = f"Delete {c.name}"
            if c.needs_confirmation:
                lost = Text("This removes the worktree and the branch. You would lose:\n", style="default")
                for item in c.lost:
                    lost.append(f"  • {item}\n", style="yellow")
                yield Static(lost, id="lost")
                yield Label(f"Type {c.name} to delete anyway")
                yield Input(placeholder=c.name, id="confirm-name")
                yield Static("", id="error", classes="warn")
                yield Static(Text.assemble(("enter", ACCENT), " delete  ", ("esc", ACCENT), " keep"), classes="hint")
            else:
                note = "stopped first, then " if c.running else ""
                yield Static(Text(f"{c.name} is clean and merged; it will be {note}removed with its worktree and "
                                  f"branch."), id="lost")
                yield Static(Text.assemble(("y", ACCENT), " delete  ", ("n", ACCENT), "/", ("esc", ACCENT), " keep"),
                             classes="hint")

    def on_mount(self) -> None:
        if self.check.needs_confirmation:
            self.query_one("#confirm-name", Input).focus()

    def on_key(self, event) -> None:
        if not self.check.needs_confirmation and event.key == "y":
            event.stop()
            self.dismiss(self.check.name)

    @on(Input.Submitted, "#confirm-name")
    def _submit(self, event: Input.Submitted) -> None:
        if event.value.strip() == self.check.name:
            self.dismiss(self.check.name)
        else:
            event.input.value = ""
            self.query_one("#error", Static).update("That is not the agent's name; nothing was deleted.")

    def action_cancel(self) -> None:
        self.dismiss(None)


class TextInputModal(ModalScreen[str | None]):
    """One-line input: talk, broadcast, filter."""

    BINDINGS = [Binding("escape", "cancel", "cancel")]

    def __init__(self, title: str, placeholder: str = "", value: str = "", hint: str = "", modal_id: str = "input"):
        super().__init__(id=modal_id)
        self.title_text = title
        self.placeholder = placeholder
        self.value = value
        self.hint = hint

    def compose(self) -> ComposeResult:
        with Vertical(classes="modal") as box:
            box.border_title = self.title_text
            yield Input(value=self.value, placeholder=self.placeholder, id="text")
            if self.hint:
                yield Static(self.hint, classes="hint")

    def on_mount(self) -> None:
        self.query_one("#text", Input).focus()

    @on(Input.Submitted)
    def _submit(self, event: Input.Submitted) -> None:
        self.dismiss(event.value)

    def action_cancel(self) -> None:
        self.dismiss(None)


class ChoiceModal(ModalScreen[str | None]):
    """A short menu: each choice has a key."""

    BINDINGS = [Binding("escape", "cancel", "cancel")]

    def __init__(self, title: str, choices: list[tuple[str, str, str]], note: str = "", modal_id: str = "choice"):
        super().__init__(id=modal_id)
        self.title_text = title
        self.choices = choices  # (key, value, label)
        self.note = note

    def compose(self) -> ComposeResult:
        with Vertical(classes="modal") as box:
            box.border_title = self.title_text
            if self.note:
                yield Static(self.note, id="note")
            body = Text()
            for key, _value, label in self.choices:
                body.append(f" {key} ", style=f"bold {ACCENT}")
                body.append(f" {label}\n")
            yield Static(body, id="choices")
            yield Static(Text.assemble(("esc", ACCENT), " cancel"), classes="hint")

    def on_key(self, event) -> None:
        for key, value, _label in self.choices:
            if event.key == key:
                event.stop()
                self.dismiss(value)
                return

    def action_cancel(self) -> None:
        self.dismiss(None)


HELP = [
    ("n", "new agent"), ("s", "stop (keeps worktree and branch)"), ("r", "restart / resume"),
    ("a / enter", "attach (Ctrl+] or Ctrl-b d detaches)"), ("p", "peek: live output in the details panel"),
    ("t", "talk to the selected agent"), ("b", "broadcast to every running agent"), ("m", "merge menu"),
    ("d", "diff against the default branch"), ("x", "delete worktree and branch"), ("/", "filter"),
    ("g", "network view"), ("f", "file map"), ("c", "comms view"), ("H", "show hidden (deleted) agents"),
    ("A", "adopt an untracked worktree"), ("q", "quit (agents keep running)"),
]


class HelpModal(ModalScreen[None]):
    BINDINGS = [Binding("escape,q,question_mark", "close", "close")]

    def __init__(self):
        super().__init__(id="help")

    def compose(self) -> ComposeResult:
        with Vertical(classes="modal") as box:
            box.border_title = "Keys"
            t = Text()
            for key, what in HELP:
                t.append(f"{key:>10}", style=f"bold {ACCENT}")
                t.append(f"  {what}\n")
            yield Static(t)

    def action_close(self) -> None:
        self.dismiss(None)


class MessageModal(ModalScreen[None]):
    BINDINGS = [Binding("escape,enter,q", "close", "close")]

    def __init__(self, title: str, text: str, summary: str = ""):
        super().__init__(id="message")
        self.title_text = title
        self.text = text
        self.summary = summary

    def compose(self) -> ComposeResult:
        with Vertical(classes="modal") as box:
            box.border_title = self.title_text
            if self.summary:
                yield Static(Text(self.summary, style="bold"))
            yield Static(Text(self.text))
            yield Static(Text.assemble(("esc", ACCENT), " close"), classes="hint")

    def action_close(self) -> None:
        self.dismiss(None)
