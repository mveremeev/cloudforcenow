"""CloudForceNow — Textual TUI for renting and managing Vast.ai machines."""

from __future__ import annotations

import re
import time
import webbrowser

from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Center, Horizontal, Middle, Vertical
from textual.screen import ModalScreen, Screen
from textual.widgets import (
    Button,
    Checkbox,
    Collapsible,
    DataTable,
    Footer,
    Input,
    Label,
    Select,
    Static,
    TabbedContent,
    TabPane,
    Tabs,
)

from .api import GPU_SERIES, REGIONS, VastClient, VastError
from .config import Config

INSTANCES_URL = "https://cloud.vast.ai/instances/"

LOGO = "  ⛅ cloud[b]force[/b]now"


class RowTable(DataTable):
    """DataTable that hands left/right over to tab switching."""

    BINDINGS = [
        Binding("left", "screen.prev_tab", "prev tab", show=False),
        Binding("right", "screen.next_tab", "next tab", show=False),
    ]


class FormSelect(Select):
    """Select that opens only on enter/space; up/down move focus instead."""

    BINDINGS = [
        Binding("down", "app.focus_next", "next field", show=False),
        Binding("up", "app.focus_previous", "prev field", show=False),
    ]


def gb(mb: float | None) -> str:
    return f"{(mb or 0) / 1024:.0f}G"


def cpu_short(name: str | None) -> str:
    """Trim marketing noise off a CPU model, e.g. 'AMD Ryzen 9 7950X'."""
    if not name:
        return "—"
    n = re.sub(r"\(R\)|\(TM\)|\(tm\)|™|®", "", name)
    n = re.sub(r"\d+-Core Processor", "", n)
    n = re.sub(r"\bwith Radeon Graphics\b", "", n, flags=re.IGNORECASE)
    n = n.replace("Processor", "").replace("CPU", "")
    n = re.sub(r"\s+", " ", n).strip()
    return n[:28] if n else "—"


def play_time(credit: float | None, dph: float | None) -> str:
    """How long `credit` lasts at `dph` $/hr, e.g. '2d 6h', '34h', '4.5h'."""
    if credit is None or not dph or dph <= 0:
        return "—"
    hours = credit / dph
    if hours >= 48:
        return f"{int(hours // 24)}d {int(hours % 24)}h"
    if hours >= 10:
        return f"{hours:.0f}h"
    if hours >= 1:
        return f"{hours:.1f}h"
    return f"{hours * 60:.0f}m"


def uptime(inst: dict) -> str:
    start = inst.get("start_date")
    if not start:
        return ""
    secs = max(0.0, time.time() - start)
    if secs >= 86400:
        return f"up {int(secs // 86400)}d {int(secs % 86400 // 3600)}h"
    if secs >= 3600:
        return f"up {int(secs // 3600)}h {int(secs % 3600 // 60)}m"
    return f"up {int(secs // 60)}m"


def offer_rows(offer: dict, credit: float | None) -> tuple:
    rel = offer.get("reliability2") or offer.get("reliability") or 0
    dph = offer.get("dph_total") or 0
    return (
        f"[b bright_blue]{offer.get('gpu_name', '?')}[/]\n"
        f"{gb(offer.get('gpu_ram'))} vram",
        f"{cpu_short(offer.get('cpu_name'))}\n"
        f"{offer.get('cpu_ghz') or 0:.1f} GHz · "
        f"{offer.get('cpu_cores_effective') or 0:.0f}c · "
        f"{gb(offer.get('cpu_ram'))} ram",
        f"{offer.get('disk_space') or 0:.0f}G disk · {rel * 100:.1f}% rel\n"
        f"{offer.get('geolocation') or '—'}",
        f"[cyan]↓{offer.get('inet_down') or 0:.0f} ↑{offer.get('inet_up') or 0:.0f}[/] mbps\n"
        f"↓{(offer.get('inet_down_cost') or 0) * 1000:.1f} "
        f"↑{(offer.get('inet_up_cost') or 0) * 1000:.1f} $/tb",
        f"[b green]${dph:.3f}[/]/hr\n"
        f"[yellow]⏱ {play_time(credit, dph)}[/] play",
    )


def instance_status(inst: dict) -> str:
    actual = (inst.get("actual_status") or "").lower()
    intended = (inst.get("intended_status") or "").lower()
    if actual == "running":
        return "[green]● running[/]"
    if actual in ("loading", "created", "") and intended == "running":
        return "[yellow]◐ starting[/]"
    if actual == "offline":
        return "[red]✗ offline[/]"
    if actual == "exited" or intended == "stopped":
        return "[bright_black]○ stopped[/]"
    return f"[bright_black]{actual or '?'}[/]"


def instance_row(inst: dict, credit: float | None) -> tuple:
    dph = inst.get("dph_total") or 0
    running = (inst.get("actual_status") or "").lower() == "running"
    left = (
        f"[yellow]⏱ {play_time(credit, dph)}[/] "
        + ("left" if running else "if resumed")
    )
    return (
        f"[b bright_blue]{inst.get('gpu_name', '?')}[/]\n"
        f"[bright_black]#{inst.get('id', '?')}[/]",
        f"{instance_status(inst)}\n"
        f"[bright_black]{uptime(inst) if running else ''}[/]",
        f"{inst.get('public_ipaddr') or '—'}\n"
        f"{inst.get('geolocation') or '—'}",
        inst.get("label") or "—",
        f"[green]${dph:.3f}[/]/hr\n{left}",
    )


class ConfirmModal(ModalScreen[bool]):
    """y/enter to confirm, n/escape to cancel."""

    BINDINGS = [
        Binding("y", "decide(True)", "yes"),
        Binding("n,escape", "decide(False)", "no"),
        Binding("down,right", "app.focus_next", "next", show=False),
        Binding("up,left", "app.focus_previous", "prev", show=False),
    ]

    def __init__(self, title: str, body: str, action: str, danger: bool = False):
        super().__init__()
        self._title = title
        self._body = body
        self._action = action
        self._danger = danger

    def compose(self) -> ComposeResult:
        with Middle(), Center():
            with Vertical(classes="modal-box" + (" danger" if self._danger else "")):
                yield Label(self._title, classes="modal-title")
                yield Static(self._body, classes="modal-body")
                with Horizontal(classes="modal-buttons"):
                    yield Button(
                        self._action,
                        id="confirm",
                        variant="error" if self._danger else "success",
                    )
                    yield Button("cancel", id="cancel")

    def action_decide(self, result: bool) -> None:
        self.dismiss(result)

    @on(Button.Pressed)
    def _pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "confirm")


class FiltersModal(ModalScreen[bool]):
    """Edit region and max $/hr; dismisses True when filters changed."""

    BINDINGS = [
        Binding("escape", "cancel", "cancel"),
        Binding("down", "app.focus_next", "next field", show=False),
        Binding("up", "app.focus_previous", "prev field", show=False),
    ]

    def __init__(self, config: Config):
        super().__init__()
        self._config = config

    def compose(self) -> ComposeResult:
        cfg = self._config
        with Middle(), Center():
            with Vertical(classes="modal-box"):
                yield Label("filters", classes="modal-title")
                yield Label("region", classes="field-label")
                yield FormSelect(
                    [(name, name) for name in REGIONS],
                    value=cfg.region if cfg.region in REGIONS else "Planet Earth",
                    allow_blank=False,
                    id="region-select",
                )
                yield Label("max $/hr", classes="field-label")
                yield Input(
                    value="" if cfg.max_dph is None else f"{cfg.max_dph:g}",
                    placeholder="no limit",
                    id="price-input",
                )
                with Collapsible(title="advanced", id="advanced"):
                    yield Label("gpu series", classes="field-label")
                    with Horizontal(id="series-row"):
                        for s in GPU_SERIES:
                            yield Checkbox(
                                s, s in cfg.gpu_series, id=f"series-{s}"
                            )
                    with Horizontal(classes="adv-row"):
                        with Vertical(classes="adv-field"):
                            yield Label("min cpu GHz", classes="field-label")
                            yield Input(value=f"{cfg.min_cpu_ghz:g}", id="ghz-input")
                        with Vertical(classes="adv-field"):
                            yield Label("min reliability %", classes="field-label")
                            yield Input(value=f"{cfg.min_reliability:g}", id="rel-input")
                    with Horizontal(classes="adv-row"):
                        with Vertical(classes="adv-field"):
                            yield Label("min disk GB", classes="field-label")
                            yield Input(value=str(cfg.min_disk), id="disk-input")
                        with Vertical(classes="adv-field"):
                            yield Label("max net $/TB", classes="field-label")
                            yield Input(value=f"{cfg.max_net_cost:g}", id="net-input")
                with Horizontal(classes="modal-buttons"):
                    yield Button("apply", id="confirm", variant="primary")
                    yield Button("cancel", id="cancel")

    def action_cancel(self) -> None:
        self.dismiss(False)

    @on(Button.Pressed, "#cancel")
    def _cancel(self) -> None:
        self.dismiss(False)

    def _number(self, selector: str, label: str) -> float | None:
        """Parse an advanced numeric field; notify and return None on error."""
        raw = self.query_one(selector, Input).value.strip()
        try:
            return float(raw) if raw else 0.0
        except ValueError:
            self.notify(f"{label} must be a number", severity="warning")
            return None

    @on(Input.Submitted)
    @on(Button.Pressed, "#confirm")
    def _apply(self) -> None:
        price_raw = self.query_one("#price-input", Input).value.strip()
        try:
            max_dph = float(price_raw) if price_raw else None
        except ValueError:
            self.notify("max $/hr must be a number", severity="warning")
            return

        ghz = self._number("#ghz-input", "min cpu GHz")
        rel = self._number("#rel-input", "min reliability")
        disk = self._number("#disk-input", "min disk")
        net = self._number("#net-input", "max net cost")
        if None in (ghz, rel, disk, net):
            return

        series = [s for s in GPU_SERIES
                  if self.query_one(f"#series-{s}", Checkbox).value]
        if not series:
            self.notify("select at least one GPU series", severity="warning")
            return

        cfg = self._config
        cfg.region = str(self.query_one("#region-select", Select).value)
        cfg.max_dph = max_dph
        cfg.gpu_series = series
        cfg.min_cpu_ghz = ghz
        cfg.min_reliability = rel
        cfg.min_disk = int(disk)
        cfg.max_net_cost = net
        cfg.save()
        self.dismiss(True)


class LoginScreen(Screen):
    """API-key login: paste key, verified against /users/current/."""

    BINDINGS = [
        Binding("down", "app.focus_next", "next field", show=False),
        Binding("up", "app.focus_previous", "prev field", show=False),
    ]

    def compose(self) -> ComposeResult:
        with Middle(), Center():
            with Vertical(id="login-box"):
                yield Static(LOGO, id="login-logo")
                yield Label("connect your vast.ai account", id="login-sub")
                yield Input(
                    password=True,
                    placeholder="paste your API key…",
                    id="key-input",
                )
                yield Button("connect", id="connect", variant="primary")
                yield Static(
                    "[bright_black]get a key at [u]cloud.vast.ai/manage-keys[/][/]",
                    id="login-hint",
                )

    @on(Input.Submitted, "#key-input")
    @on(Button.Pressed, "#connect")
    def _submit(self) -> None:
        key = self.query_one("#key-input", Input).value.strip()
        if key:
            self.verify(key)

    @work(exclusive=True)
    async def verify(self, key: str) -> None:
        app: CFNApp = self.app  # type: ignore[assignment]
        client = VastClient(key)
        try:
            user = await client.current_user()
        except VastError as exc:
            await client.close()
            self.notify(f"login failed: {exc}", severity="error")
            return
        if app.client:
            await app.client.close()
        app.client = client
        app.config.api_key = key
        app.config.save()
        self.notify(f"welcome, {user.get('username') or user.get('email') or 'pilot'}")
        app.switch_screen(MainScreen())


class MainScreen(Screen):
    BINDINGS = [
        Binding("1", "show_tab('tab-offers')", "offers", show=False),
        Binding("2", "show_tab('tab-instances')", "instances", show=False),
        Binding("f", "filters", "filters"),
        Binding("r", "refresh", "refresh"),
        Binding("enter", "rent", "rent", show=False),
        Binding("s", "start", "start"),
        Binding("x", "stop", "stop"),
        Binding("b", "reboot", "reboot"),
        Binding("d", "destroy", "destroy"),
        Binding("o", "open_web", "open in browser"),
    ]

    OFFER_COLUMNS = ("gpu", "cpu", "host", "network", "price · play")
    INSTANCE_COLUMNS = ("gpu", "status", "ip · loc", "label", "price · play")

    def __init__(self) -> None:
        super().__init__()
        self.offers: list[dict] = []
        self.instance_list: list[dict] = []
        self.credit: float | None = None

    # -- layout ---------------------------------------------------------

    def compose(self) -> ComposeResult:
        with Horizontal(id="topbar"):
            yield Static(LOGO, id="brand")
            yield Static("", id="account")
        with TabbedContent(initial="tab-offers"):
            with TabPane("offers", id="tab-offers"):
                yield Static("", id="offers-line", classes="statusline")
                yield RowTable(id="offers-table", cursor_type="row")
            with TabPane("instances", id="tab-instances"):
                yield Static("", id="instances-line", classes="statusline")
                yield RowTable(id="instances-table", cursor_type="row")
        yield Footer()

    @property
    def app_config(self) -> Config:
        return self.app.config  # type: ignore[attr-defined]

    @property
    def client(self) -> VastClient:
        return self.app.client  # type: ignore[attr-defined]

    def on_mount(self) -> None:
        offers = self.query_one("#offers-table", DataTable)
        offers.add_columns(*self.OFFER_COLUMNS)
        instances = self.query_one("#instances-table", DataTable)
        instances.add_columns(*self.INSTANCE_COLUMNS)
        offers.cell_padding = 3
        instances.cell_padding = 3
        self.query_one(Tabs).can_focus = False
        self.load_account()
        self.load_offers()
        self.load_instances()
        self.set_interval(10.0, self._poll)

    def _poll(self) -> None:
        self.load_instances(quiet=True)

    def _focus_table(self, table: DataTable, tab: str) -> None:
        """Put keyboard focus on a tab's table, but never steal it from a modal.

        TabbedContent focuses its tab bar on mount and the loading spinner
        blurs the table, so focus has to be restored once data is in.
        """
        if self.app.screen is self and self._active_tab() == tab:
            table.focus()

    def _fit_columns(self, table: DataTable) -> None:
        """Stretch columns to fill the table's width (DataTable content-sizes
        them by default and leaves the rest of the row blank)."""
        cols = list(table.columns.values())
        avail = table.scrollable_content_region.width
        if not cols or avail <= 0:
            return
        pad = table.cell_padding
        natural = sum(2 * pad + c.content_width for c in cols)
        slack = avail - natural
        if slack <= 0:  # content wider than the pane: size naturally, allow scroll
            for c in cols:
                c.auto_width = True
            table.styles.overflow_x = "auto"
        else:
            share, extra = divmod(slack, len(cols))
            for i, c in enumerate(cols):
                c.auto_width = False
                c.width = c.content_width + share + (1 if i < extra else 0)
            # Columns now sum to exactly `avail`; hide overflow so a rounding
            # cell can't spawn a phantom horizontal scrollbar.
            table.styles.overflow_x = "hidden"
        table._require_update_dimensions = True
        table.refresh()

    def _fit_all(self) -> None:
        for tid in ("#offers-table", "#instances-table"):
            self._fit_columns(self.query_one(tid, DataTable))

    def on_resize(self) -> None:
        self.call_after_refresh(self._fit_all)

    # -- data loading -----------------------------------------------------

    @work(exclusive=True, group="account")
    async def load_account(self) -> None:
        try:
            user = await self.client.current_user()
        except VastError as exc:
            if exc.status == 401:
                self.notify("API key rejected — please log in again", severity="error")
                self.app.switch_screen(LoginScreen())
            return
        name = user.get("username") or user.get("email") or "?"
        credit = user.get("credit", user.get("balance", 0)) or 0
        self.query_one("#account", Static).update(
            f"[bright_black]{name}[/]  [b green]${credit:.2f}[/] "
        )
        if credit != self.credit:  # re-render play-time columns
            self.credit = credit
            self._render_offers()
            self._render_instances()

    @work(exclusive=True, group="offers")
    async def load_offers(self) -> None:
        table = self.query_one("#offers-table", DataTable)
        table.loading = True
        cfg = self.app_config
        try:
            self.offers = await self.client.search_offers(
                REGIONS.get(cfg.region, []),
                cfg.max_dph,
                gpu_series=cfg.gpu_series,
                min_cpu_ghz=cfg.min_cpu_ghz,
                min_reliability=cfg.min_reliability,
                min_disk=cfg.min_disk,
                max_net_cost=cfg.max_net_cost,
            )
        except VastError as exc:
            self.notify(f"offer search failed: {exc}", severity="error")
            return
        finally:
            table.loading = False
        self._render_offers()
        price = f"≤ ${cfg.max_dph:g}/hr" if cfg.max_dph else "any price"
        self.query_one("#offers-line", Static).update(
            f"{cfg.region.lower()} · {price} · "
            f"[b]{len(self.offers)}[/b] machines "
            f"[bright_black]· ubuntu desktop VM[/]"
        )
        self._focus_table(table, "tab-offers")

    def _render_offers(self) -> None:
        table = self.query_one("#offers-table", DataTable)
        row = table.cursor_row or 0
        table.clear()
        # Third line stays blank: separates entries and pads the cursor bar.
        for offer in self.offers:
            table.add_row(*offer_rows(offer, self.credit), height=3)
        if self.offers:
            table.move_cursor(row=min(row, len(self.offers) - 1))
        self.call_after_refresh(self._fit_columns, table)

    @work(exclusive=True, group="instances")
    async def load_instances(self, quiet: bool = False) -> None:
        table = self.query_one("#instances-table", DataTable)
        if not quiet:
            table.loading = True
        try:
            self.instance_list = await self.client.instances()
        except VastError as exc:
            if not quiet:
                self.notify(f"could not load instances: {exc}", severity="error")
            return
        finally:
            if not quiet:
                table.loading = False
        self._render_instances()
        count = len(self.instance_list)
        self.query_one("#instances-line", Static).update(
            f"[b]{count}[/b] instance{'' if count == 1 else 's'}   "
            "[bright_black]auto-refreshes every 10s[/]"
            if count
            else "[bright_black]no instances — press ← and rent a machine[/]"
        )
        if not quiet:
            self._focus_table(table, "tab-instances")

    def _render_instances(self) -> None:
        table = self.query_one("#instances-table", DataTable)
        row = table.cursor_row or 0
        table.clear()
        for inst in self.instance_list:
            table.add_row(*instance_row(inst, self.credit), height=3)
        if self.instance_list:
            table.move_cursor(row=min(row, len(self.instance_list) - 1))
        self.call_after_refresh(self._fit_columns, table)

    # -- helpers ----------------------------------------------------------

    def _active_tab(self) -> str:
        return self.query_one(TabbedContent).active

    def _selected_offer(self) -> dict | None:
        table = self.query_one("#offers-table", DataTable)
        if not self.offers or table.cursor_row >= len(self.offers):
            return None
        return self.offers[table.cursor_row]

    def _selected_instance(self) -> dict | None:
        table = self.query_one("#instances-table", DataTable)
        if not self.instance_list or table.cursor_row >= len(self.instance_list):
            return None
        return self.instance_list[table.cursor_row]

    # -- actions ------------------------------------------------------------

    def action_show_tab(self, tab: str) -> None:
        self.query_one(TabbedContent).active = tab
        table_id = "#offers-table" if tab == "tab-offers" else "#instances-table"
        self.query_one(table_id, DataTable).focus()

    def action_prev_tab(self) -> None:
        self._toggle_tab()

    def action_next_tab(self) -> None:
        self._toggle_tab()

    def _toggle_tab(self) -> None:
        other = (
            "tab-instances"
            if self._active_tab() == "tab-offers"
            else "tab-offers"
        )
        self.action_show_tab(other)

    def action_filters(self) -> None:
        self.app.push_screen(
            FiltersModal(self.app_config),
            lambda changed: self.load_offers() if changed else None,
        )

    def action_refresh(self) -> None:
        if self._active_tab() == "tab-offers":
            self.load_offers()
        else:
            self.load_instances()
        self.load_account()

    @on(DataTable.RowSelected, "#offers-table")
    def _offer_selected(self) -> None:
        self.action_rent()

    def action_rent(self) -> None:
        if self._active_tab() != "tab-offers":
            return
        offer = self._selected_offer()
        if offer is None:
            return
        dph = offer.get("dph_total") or 0
        body = (
            f"[b bright_blue]{offer.get('gpu_name')}[/] · "
            f"{gb(offer.get('gpu_ram'))} vram · "
            f"{offer.get('cpu_ghz') or 0:.1f} GHz · "
            f"{offer.get('geolocation') or '?'}\n"
            f"template [i]{self.app_config.template_name}[/i], "
            f"{self.app_config.disk} GB disk\n"
            f"[b green]${dph:.3f}/hr[/] · "
            f"[yellow]⏱ {play_time(self.credit, dph)} of play[/] on your balance"
        )
        self.app.push_screen(
            ConfirmModal("rent this machine?", body, "rent"),
            lambda ok: self.rent(offer) if ok else None,
        )

    @work(group="rent")
    async def rent(self, offer: dict) -> None:
        cfg = self.app_config
        try:
            if not cfg.template_hash:
                cfg.template_hash = await self.client.find_template_hash(
                    cfg.template_name
                )
                cfg.save()
            result = await self.client.create_instance(
                offer["id"], cfg.template_hash, cfg.disk, label="cloudforcenow"
            )
        except VastError as exc:
            self.notify(f"rent failed: {exc}", severity="error")
            return
        new_id = (result or {}).get("new_contract", "?")
        self.notify(f"instance {new_id} is spinning up ✦", timeout=5)
        self.action_show_tab("tab-instances")
        self.load_instances(quiet=True)

    def _instance_op(self, op: str) -> None:
        inst = self._selected_instance()
        if self._active_tab() != "tab-instances" or inst is None:
            return
        if op == "destroy":
            body = (
                f"instance [b]{inst.get('id')}[/] · {inst.get('gpu_name')}\n"
                "[red]this deletes the instance and its disk, permanently[/]"
            )
            self.app.push_screen(
                ConfirmModal("destroy instance?", body, "destroy", danger=True),
                lambda ok: self.run_op(op, inst) if ok else None,
            )
        else:
            self.run_op(op, inst)

    @work(group="ops")
    async def run_op(self, op: str, inst: dict) -> None:
        instance_id = inst["id"]
        call = {
            "start": self.client.start_instance,
            "stop": self.client.stop_instance,
            "reboot": self.client.reboot_instance,
            "destroy": self.client.destroy_instance,
        }[op]
        try:
            await call(instance_id)
        except VastError as exc:
            self.notify(f"{op} failed: {exc}", severity="error")
            return
        self.notify(f"{op} sent to instance {instance_id}", timeout=3)
        self.load_instances(quiet=True)

    def action_start(self) -> None:
        self._instance_op("start")

    def action_stop(self) -> None:
        self._instance_op("stop")

    def action_reboot(self) -> None:
        self._instance_op("reboot")

    def action_destroy(self) -> None:
        self._instance_op("destroy")

    def action_open_web(self) -> None:
        webbrowser.open(INSTANCES_URL)
        self.notify("opened cloud.vast.ai/instances", timeout=2)


class CFNApp(App):
    TITLE = "CloudForceNow"
    CSS_PATH = "cfn.tcss"

    def __init__(self) -> None:
        super().__init__()
        self.config = Config.load()
        self.client: VastClient | None = None

    def on_mount(self) -> None:
        # ANSI theme: every colour comes from the terminal's own palette.
        self.theme = "ansi-dark"
        if self.config.api_key:
            self.client = VastClient(self.config.api_key)
            self.push_screen(MainScreen())
        else:
            self.push_screen(LoginScreen())

    async def on_unmount(self) -> None:
        if self.client:
            await self.client.close()


def main() -> None:
    CFNApp().run()


if __name__ == "__main__":
    main()
