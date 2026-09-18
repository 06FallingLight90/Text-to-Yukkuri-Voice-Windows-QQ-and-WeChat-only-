"""The built-in troubleshooting page, opened from the underlined 「帮助」.

The content is data, not code: :data:`SECTIONS` is a tuple of tables, each row a
tuple of cells. Keeping it separate from the rendering means the text can be
checked by tests that never open a window, and the page can be reviewed without
reading widget code.

Nothing here touches the chat clients, the audio system or the network - the page
is plain text and is safe to open at any time, including in the middle of a send.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

from dataclasses import dataclass

import customtkinter as ctk
import tkinter as tk

from theme import (
    BORDER,
    CARD,
    FONT_FAMILY,
    MUTED,
    PRIMARY,
    PRIMARY_HOVER,
    SURFACE,
    TEXT,
)

#: Star marks the rows that solve most reports in under a minute.
STAR = "★"

#: Column proportions for a three-column table, and for the two-column ones.
THREE_COLUMN_WEIGHTS = (26, 34, 40)
TWO_COLUMN_WEIGHTS = (36, 64)


@dataclass(frozen=True)
class Table:
    """One table on the help page."""

    title: str
    headers: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...]
    note: str = ""

    @property
    def weights(self) -> tuple[int, ...]:
        if len(self.headers) == 2:
            return TWO_COLUMN_WEIGHTS
        return THREE_COLUMN_WEIGHTS


SECTIONS: tuple[Table, ...] = (
    Table(
        title="先看这里",
        headers=("你看到的提示", "先查这几条"),
        rows=(
            (
                "没有找到微信 / QQ 窗口",
                "窗口被隐藏（微信 4.x 的「最小化」= 隐藏）· 开了多个聊天窗口 · "
                "客户端和本程序权限不一致 —— 见通用表",
            ),
            (
                "点击后微信没有开始录音",
                "安全软件的麦克风询问窗压在微信上（只有微信会这样）· "
                "按钮位置变了要重标 · 别的程序占着录音端点 —— 见微信表",
            ),
            (
                "发出去的语音是空的",
                "Windows 没允许桌面应用用麦克风 · 默认设备/通信设备不对 · "
                "别的程序占着麦克风 —— 见通用表、QQ 表",
            ),
            (
                "QQ 还没有标定过坐标",
                "QQ 没有可用的默认坐标，程序会直接拒绝发送 —— 见 QQ 表",
            ),
        ),
    ),
    Table(
        title="一、通用：系统、权限、设备（微信和 QQ 都适用）",
        headers=("现象", "可能原因", "怎么办"),
        rows=(
            (
                f"{STAR} 第一次发送时，被「是否允许使用麦克风」的询问窗口拦下",
                "安全软件（火绒、360 等）第一次会问本程序能不能用麦克风，"
                "那个窗口会压在聊天客户端上，客户端就一直等麦克风",
                "把询问窗口点成「允许」，再发一次即可。权限只需要给一次。"
                "（微信上还会表现为语音键变灰、转圈，见微信表）",
            ),
            (
                f"{STAR} 发出去的语音是空的、只有环境噪音",
                "① Windows 没允许桌面应用使用麦克风 "
                "② 默认录音设备被别的程序占着",
                "① 设置 →「隐私和安全性」→「麦克风」→ 打开"
                "「允许桌面应用访问麦克风」 "
                "② 关掉 OBS、录音软件、会议软件等占用麦克风的程序再试",
            ),
            (
                f"{STAR} 客户端明明开着，却说「没有找到窗口」",
                "① 客户端被「最小化」隐藏了（不是缩小到任务栏） "
                "② 开了多个聊天窗口 "
                "③ 客户端以管理员身份运行、本程序不是，读不到进程名",
                "① 从托盘或任务栏点开客户端主窗口 "
                "② 只留一个要发送的聊天窗口 "
                "③ 两边权限设成一样；`python tools\\list_windows.py` 会逐条说明原因",
            ),
            (
                "提示「已取消发送以避免点错窗口」",
                "发送那两三秒里你在别的窗口点了鼠标或打字，前台被抢走了",
                "发送时不要操作别的窗口，然后重试",
            ),
            (
                "每次点击都固定偏向同一个方向、同一段距离",
                "坐标本身不准（不是窗口位置的问题）",
                "打开设置，点「重新标定」",
            ),
            (
                "改过显示缩放或分辨率之后开始不准",
                "缩放变化会改变客户端内部按钮的实际位置",
                "重新标定一次",
            ),
            (
                "提示「VB-CABLE 播放端不可用」",
                "VB-CABLE 没安装、装完没重启 Windows、或者被禁用了",
                "重装 VB-CABLE，然后重启 Windows",
            ),
            (
                "提示「无法查询 Windows 音频会话列表」",
                "Windows 音频服务刚重启或崩溃，或者默认录音端点消失了"
                "（驱动更新、USB 麦克风被拔掉）",
                "等几秒重试；确认「声音设置」里能看到录音设备；必要时重启 Windows",
            ),
            (
                "在远程桌面（RDP）里运行",
                "RDP 会隔离本机的虚拟声卡端点，客户端看不到它",
                "在本机控制台运行，或改用不隔离音频的远控方式",
            ),
            (
                "提示「当前前台窗口不是微信 / QQ」",
                "客户端没能切到前台（被别的窗口挡住，或者它自己弹了窗）",
                "手动点开目标聊天窗口，再发一次",
            ),
            (
                "快捷键按了没反应",
                "组合被别的程序占用了，或者设置里是空的",
                "在设置里换一个组合，或改用托盘菜单发送",
            ),
            (
                "提示「找不到 Node.js」/「合成依赖未安装」",
                "缺少运行环境",
                "安装 Node.js 18+；在 `synth` 目录执行 `npm install`",
            ),
            (
                "提示翻译失败（401 / 202 / 108 / 110 等）",
                "API Key、模型名、有道的 appKey / appSecret 不对，"
                "或者没有开通对应服务",
                "设置 →「翻译方式」里核对；有道要在控制台开通「文本翻译」服务",
            ),
            (
                "点「试听本次语音」没有声音",
                "试听走的是本机扬声器，和虚拟声卡、聊天客户端都无关",
                "检查系统音量和输出设备。试听不会发送任何东西",
            ),
        ),
    ),
    Table(
        title="二、微信专有",
        headers=("现象", "可能原因", "怎么办"),
        rows=(
            (
                f"{STAR} 点「发送语音」后，微信的语音按钮变灰、中间转圈，"
                "随后提示「点击后微信没有开始录音」",
                "安全软件（火绒、360 等）第一次询问麦克风权限，那个弹窗正好压在"
                "微信上，微信一直在等麦克风 —— 这个表现只有微信有，QQ 不会被这样拦",
                "把询问窗口点成「允许」，再发一次",
            ),
            (
                "提示「点击后微信没有开始录音」（没有变灰转圈）",
                "① 那个位置不是「语音」按钮 —— 微信更新后按钮可能挪了 "
                "② 别的程序占着 CABLE Output，微信录不了音",
                "① 打开设置点「重新标定」 "
                "② `python tools\\check_default_devices.py` 看端点状态，"
                "并关掉占用麦克风的程序",
            ),
            (
                "提示「找不到微信主窗口」",
                "微信 4.1.15.11 起主窗口标题从「微信」变成了账号名"
                "（程序已适配：标题优先、窗口类兜底）；也可能是窗口被隐藏或多开",
                "点开主窗口、只留一个聊天窗口；"
                "`python tools\\list_windows.py` 会逐条说明每个窗口是否满足条件",
            ),
            (
                "语音按钮或发送按钮点了没反应，或者点到了旁边的按钮",
                "微信更新、或显示缩放变化，导致标定的坐标失准",
                "打开设置，点「重新标定」",
            ),
            (
                "语音气泡发出去了，但是里面是空的",
                "录音没有走 CABLE Output",
                "见通用表「语音是空的」那一条",
            ),
            (
                "正在微信语音/视频通话，或自己按着语音键时点了发送",
                "程序分不清麦克风是它自己打开的，还是你在通话、手动录音",
                "先结束通话、松开语音键再发送。否则可能把你正在录的那段发出去",
            ),
            (
                "换了浅色/深色模式、换了皮肤或字体",
                "旧版本会受影响（那时靠数绿色像素判断录音状态，已废弃）",
                "现在不受影响，无需处理",
            ),
        ),
    ),
    Table(
        title="三、QQ 专有",
        headers=("现象", "可能原因", "怎么办"),
        rows=(
            (
                f"{STAR} 提示「QQ 还没有标定过坐标」",
                "QQ 没有可用的默认坐标：它的按钮挨得很近，猜错会点到红包之类的控件，"
                "所以程序宁可拒绝发送",
                "设置 →「发送到」选中 QQ → 点「重新标定」，标「按住说话」按钮",
            ),
            (
                f"{STAR} 发出去的语音是空的",
                "QQ 录的是「默认**通信**设备」，而程序临时切的是「默认设备」",
                "把 `CABLE Output` 同时设为默认设备**和**默认通信设备；"
                "或打开「发送时自动切换录音设备」；"
                "`python tools\\check_default_devices.py` 可以自查",
            ),
            (
                "点了 QQ 的「语音消息」，但程序没反应",
                "坐标是在另一种界面模式（效率模式 / 经典模式）下标的",
                "在当前界面模式下重新标定一次",
            ),
            (
                "提示「这份坐标是旧版本标的，还没有记录界面模式」",
                "老坐标没有记录界面模式，可能误选 QQ 的设置、群文件窗口",
                "在设置里点一次「重新标定」补上",
            ),
            (
                "程序选错了 QQ 窗口，或者压住了别的窗口",
                "经典模式下每个聊天都是独立窗口，主面板不算",
                "只留要发送的那个聊天窗口在最前面；标定时也用同一个窗口",
            ),
        ),
    ),
    Table(
        title="四、自查工具与反馈",
        headers=("想确认什么", "怎么做"),
        rows=(
            (
                "客户端窗口为什么没被找到",
                "`python tools\\list_windows.py`（查 QQ 加 `--target qq`）",
            ),
            (
                "默认录音/播放设备、以及自动切换是否可用",
                "`python tools\\check_default_devices.py`",
            ),
            (
                "发送链路的耗时预算、音频后端",
                "`python tools\\measure_latency.py`",
            ),
            (
                "录音开头到底有多少静音",
                "`python tools\\measure_leading_silence.py`",
            ),
            (
                "重新标定坐标",
                "程序设置页 →「重新标定」（标定当前选中的目标）",
            ),
            (
                "出问题时的完整记录",
                "`%USERPROFILE%\\.youkuli-chaspeak\\widget.log`"
                "（超过 1 MB 自动滚动，保留最近两段）",
            ),
        ),
        note="反馈问题时请附上：日志文件 + 客户端名称与版本号（在客户端「关于」里看）"
        "+ 弹窗提示的原文。",
    ),
)


class HelpWindow(ctk.CTkToplevel):
    """The troubleshooting page.

    Not modal: the point of the page is to be read *while* fixing something, so
    the main window stays usable. Only one is opened at a time (see
    ``WidgetApp.open_help``), and Escape closes it.
    """

    PAGE_PAD = 26
    CELL_PAD = 14

    def __init__(self, owner) -> None:
        super().__init__(owner.root)
        self.owner = owner
        self._cells: list[tuple[tk.Label, tuple[int, ...], int]] = []
        self._last_width = 0

        self.title("帮助：常见问题与自查")
        # Wider than tall on purpose: three columns of Chinese text turn into
        # unreadable slivers in a narrow window.
        self.geometry("1080x800")
        self.minsize(900, 560)
        self.resizable(True, True)
        self.transient(owner.root)
        self.attributes("-topmost", bool(owner.pinned))
        self.configure(fg_color=SURFACE)

        header = ctk.CTkFrame(self, fg_color="transparent")
        header.pack(fill="x", padx=self.PAGE_PAD, pady=(20, 12))
        ctk.CTkLabel(
            header,
            text="帮助：常见问题与自查",
            font=ctk.CTkFont(FONT_FAMILY, 20, "bold"),
            text_color=TEXT,
        ).pack(anchor="w")
        ctk.CTkLabel(
            header,
            text=f"先在弹窗提示里找关键词，再到对应表格里找。带 {STAR} 的先看，"
            "大部分问题四条之内就能解决。本页不会影响正在进行的发送。",
            font=ctk.CTkFont(FONT_FAMILY, 12),
            text_color=MUTED,
            justify="left",
            wraplength=980,
        ).pack(anchor="w", pady=(4, 0))

        actions = ctk.CTkFrame(self, fg_color="transparent")
        actions.pack(side="bottom", fill="x", padx=self.PAGE_PAD, pady=(10, 18))
        ctk.CTkButton(
            actions,
            text="关闭",
            width=110,
            height=40,
            corner_radius=12,
            fg_color=PRIMARY,
            hover_color=PRIMARY_HOVER,
            font=ctk.CTkFont(FONT_FAMILY, 13, "bold"),
            command=self.destroy,
        ).pack(side="right")
        ctk.CTkLabel(
            actions,
            text="按 Esc 也可以关闭",
            font=ctk.CTkFont(FONT_FAMILY, 11),
            text_color=MUTED,
        ).pack(side="right", padx=(0, 12))

        body = ctk.CTkScrollableFrame(
            self,
            fg_color="transparent",
            scrollbar_button_color="#C8D8CF",
            scrollbar_button_hover_color="#AFC5B8",
        )
        body.pack(fill="both", expand=True, padx=(self.PAGE_PAD, self.PAGE_PAD - 8))
        self._build_tables(body)

        self.bind("<Escape>", lambda _event: self.destroy())
        self.bind("<Configure>", self._on_resize)
        self.after(80, self._relayout)

    def _build_tables(self, parent: ctk.CTkFrame) -> None:
        for table in SECTIONS:
            ctk.CTkLabel(
                parent,
                text=table.title,
                font=ctk.CTkFont(FONT_FAMILY, 15, "bold"),
                text_color=TEXT,
            ).pack(anchor="w", pady=(14, 6))
            if table.note:
                ctk.CTkLabel(
                    parent,
                    text=table.note,
                    font=ctk.CTkFont(FONT_FAMILY, 11),
                    text_color=MUTED,
                    justify="left",
                    wraplength=980,
                ).pack(anchor="w", pady=(0, 6))

            card = ctk.CTkFrame(
                parent,
                fg_color=CARD,
                corner_radius=14,
                border_width=1,
                border_color=BORDER,
            )
            card.pack(fill="x", pady=(0, 6))
            for column, weight in enumerate(table.weights):
                card.grid_columnconfigure(column, weight=weight)

            for column, heading in enumerate(table.headers):
                cell = ctk.CTkLabel(
                    card,
                    text=heading,
                    font=ctk.CTkFont(FONT_FAMILY, 12, "bold"),
                    text_color=TEXT,
                    anchor="w",
                    justify="left",
                )
                cell.grid(
                    row=0, column=column, sticky="nw",
                    padx=(self.CELL_PAD, 8), pady=(12, 8),
                )
            self._separator(card, 1, len(table.headers))

            for index, row in enumerate(table.rows):
                grid_row = 2 + index * 2
                for column, text in enumerate(row):
                    cell = tk.Label(
                        card,
                        text=text,
                        bg=CARD,
                        fg=TEXT,
                        font=(FONT_FAMILY, 11),
                        justify="left",
                        anchor="nw",
                        bd=0,
                        highlightthickness=0,
                    )
                    cell.grid(
                        row=grid_row, column=column, sticky="nsew",
                        padx=(self.CELL_PAD, 8), pady=(9, 9),
                    )
                    # Each cell remembers its own table's proportions: a
                    # two-column table and a three-column one share the window.
                    self._cells.append((cell, table.weights, column))
                self._separator(card, grid_row + 1, len(table.headers))

    def _separator(self, card: ctk.CTkFrame, row: int, columns: int) -> None:
        line = ctk.CTkFrame(card, height=1, fg_color=BORDER, corner_radius=0)
        line.grid(row=row, column=0, columnspan=columns, sticky="ew", padx=10)

    def _on_resize(self, event: tk.Event) -> None:
        if event.widget is self:
            self._relayout()

    def _relayout(self) -> None:
        """Give every cell a wraplength that matches its column's real width."""
        width = self.winfo_width()
        if abs(width - self._last_width) < 8:
            return
        self._last_width = width
        available = width - self.PAGE_PAD * 2 - self.CELL_PAD * 2 - 26
        if available <= 200:
            return
        for cell, weights, column in self._cells:
            share = available * weights[column] / sum(weights)
            cell.configure(wraplength=max(120, int(share) - 16))


__all__ = [
    "SECTIONS",
    "STAR",
    "HelpWindow",
    "Table",
]
