# 鳄鱼派 · 沪深300 基差悬浮窗

![Version](https://img.shields.io/badge/version-v1.0.0-orange)
![Python](https://img.shields.io/badge/Python-3.8%2B-blue)
![Platform](https://img.shields.io/badge/平台-Windows%2010%2F11-lightgrey)
![License](https://img.shields.io/badge/license-MIT-green)
![Deps](https://img.shields.io/badge/依赖-requests%20only-informational)

> **鳄鱼派**出品的巴掌大置顶小窗：只把「沪深300 现货 − 持仓量加权 IF」这一个数钉在屏幕角落。
> **按 Win+D 显示桌面也不会被最小化**（原生实现，不用装 AutoHotkey）。

![预览](docs/hero.png)

左边是默认的紧凑态（248×124），右边点 ▾ 展开后的详情态。

---

## 为什么需要它

在文华/同花顺里看「300股指加权」要开一堆窗口、来回切页，而盘中真正想瞄一眼的往往只有一个数：**基差**。

基差是期现之间的「情绪计」：机构用 IF 做空对冲，会把期货价格压到现货之下；对冲盘一撤，贴水就开始收敛。盯着这一个数，有时比刷十条新闻更直接。

这个工具把它单独拿出来，做成一个不挡事的小浮窗：

- 只占 248×124 像素，贴在屏幕角落不碍事；
- 始终置顶，切到任何软件都还在；
- **Win+D 也不会消失**（这正是很多人做置顶工具时踩的坑）；
- 想细看时点一下 ▾，完整展开。

## 特性

- **只显示一个数**：基差点数 + 基差率 + 升贴水方向，红正绿负，一眼可读
- **展开即详情**：现货开/高/低、涨跌，期货加权价与涨跌，每个 IF 合约的最新价 / 涨跌 / 持仓 / 权重
- **不被 Win+D 干掉**：给窗口加上 `WS_EX_TOOLWINDOW` 扩展样式（论坛里那个 AHK `winset, ExStyle, ^0x80` 方案的原生版），天然排除在「显示桌面」之外
- **不占任务栏、不出现在 Alt+Tab**：`WS_EX_TOOLWINDOW` 顺带解决
- **自动换月**：按 CFFEX 当前挂牌的合约动态取数，自动跳过已到期合约，不用每月改代码
- **持仓量加权**：`Σ(最新价 × 持仓量) / Σ持仓量`，对齐文华「300股指加权」口径（**不是**成交量加权）
- **状态一眼可见**：绿点=实时，黄点=收盘/非交易时段，红点=取数失败重试中
- **无控制台黑窗**：双击 .py 会自动用 `pythonw` 重启自己
- **单实例**：重复双击不会开出第二个窗口
- **记忆**：位置、展开状态、刷新间隔都记住
- **开机自启**：右键菜单一键开关（写入「启动」文件夹快捷方式）

## 快速开始

**环境**：Windows 10/11 + Python 3.8 以上

```bat
:: 1. 安装唯一依赖
pip install requests

:: 2. 运行（或者直接双击 鳄鱼派_沪深300基差.py）
python 鳄鱼派_沪深300基差.py
```

第一次双击时如果闪过一个黑窗，是 `python` 启动自身的控制台，脚本会立刻用 `pythonw` 无控制台重启，之后不再出现。

> 想固定在一个位置常驻：右键 →「开机自启动」打勾。

## 使用说明

| 操作 | 效果 |
| --- | --- |
| 点右上角 `▾` / `▴` | 展开 / 收起详情 |
| 双击窗体空白处 | 同样展开 / 收起 |
| 按住窗体拖动 | 移动位置（退出后位置会记住） |
| 点右上角 `✕` | 退出 |
| 右键 | 菜单：展开/收起、刷新间隔、立即刷新、始终置顶、开机自启动、回到屏幕右上角、退出 |

颜色约定：**红 = 基差为正（现货升水 / 期货贴水），绿 = 基差为负（期货升水）**，跟随 A 股习惯。

## 基差怎么看（核心用法）

基差 = 现货 − 期货，本质上反映的是**机构对冲盘的拥挤程度**：

| 基差状态 | 通常意味着 |
| --- | --- |
| 基差为正（现货升水 / 期货贴水） | 期货比现货便宜，多半是套保空头（机构对冲盘）把远月压下去了，隐含偏谨慎甚至一致看空的预期 |
| 基差为负（期货升水） | 期货比现货贵，多头情绪较强；分红、资金成本等因素也会把升水推高 |
| **贴水持续收敛（基差回升）** | **机构集中做空对冲的避险情绪在消退，不再一致看空后市，市场风险偏好会逐步从低位修复** |
| 贴水持续走阔 | 对冲盘在加码，避险情绪升温，通常出现在恐慌或流动性紧张的阶段 |

一句话口诀：**无论指数上涨还是下跌，只要基差（贴水）在收敛，都是好事** —— 说明恐慌式的对冲在退潮，愿意裸多的人变多了，情绪正在从低位修复。

极端贴水往往对应阶段性的悲观共识底部；贴水快速收敛，常常是反弹的前奏。

> 小提醒：基差还受 **分红**（IF 分红季贴水天然放大）、**到期日临近**、**资金成本** 影响，别只盯一个数，配合成交量和持仓变化看更稳。

## 口径与算法

```
基差      = 沪深300 现货指数 − IF 持仓量加权价
加权价    = Σ(合约最新价 × 合约持仓量) / Σ合约持仓量
覆盖合约  = 当月、下月、当季、下季（自动探测，自动过滤已到期）
数据源    = 新浪财经免费行情  hq.sinajs.cn
            现货 sh000300，期货 CFF_RE_IF2601 这类代码
刷新频率  = 默认 3 秒（可在右键菜单改成 1/2/3/5/10/30 秒）
```

**关于抖动**：现货指数约 5 秒一个快照，期货是 tick 级，两边时点不完全对齐，所以基差会跳几分钱。文华终端里同样会跳，属正常现象。

**关于非交易时段**：收盘后显示当日收盘数据并标记「收盘」；周末/节假日显示上一交易日数据，状态点变黄，不会假装还在跳。

## 技术细节：为什么它不会被 Win+D 最小化

Win+D（显示桌面）走的是 Shell 的「显示桌面」逻辑，它会遍历顶层窗口并最小化；而带 `WS_EX_TOOLWINDOW`（`0x00000080`）扩展样式的窗口会被当作「工具窗口」，**不进任务栏、不进 Alt+Tab、也被排除在显示桌面之外**。

```python
style = GetWindowLongW(hwnd, GWL_EXSTYLE)
SetWindowLongW(hwnd, GWL_EXSTYLE, (style | 0x80) & ~0x00040000)  # 加 TOOLWINDOW，去 APPWINDOW
SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_FRAMECHANGED)
```

这就是那篇帖子里 `winset, AlwaysOnTop, , A` + `winset, ExStyle, ^0x80, A` 两行 AHK 做的事情。区别是：这里用 `ctypes` 直接调用 `user32`，不用额外装 AutoHotkey、不用常驻脚本。

另外还有一个 900ms 的「保活」检查：万一窗口被其他软件挤掉、被最小化、被藏起来，会自动恢复置顶与可见，作为兜底。

## 常见问题

**Q：跑起来没看到窗口？**
多显示器 / 高 DPI 缩放时可能落在看不见的位置。右键菜单 →「回到屏幕右上角」，或删掉 `%LOCALAPPDATA%\HS300BasisWidget\settings.json` 后重启。

**Q：显示「行情获取失败」？**
新浪免费接口偶尔抽风或断网。脚本每 3 秒自动重试并重新建连，恢复后自动正常，不用管。

**Q：需要开户 / 付费行情吗？**
不需要。数据全部来自新浪财经公开接口，免费、无需账号。

**Q：会被杀毒软件拦吗？**
脚本只调用了 `user32` 的 4 个函数（取/设窗口样式、置顶、检测最小化），不注入进程、不 Hook 键盘，代码全部可见，可自行审查。开机会在「启动」文件夹里创建一个快捷方式（可在菜单里关掉）。

**Q：想改外观 / 尺寸 / 刷新频率？**
文件顶部有可调参数：

```python
REFRESH_SEC = 3                    # 默认刷新间隔（秒）
EXTRA_QUARTERS = 3                 # 探测后续季月合约数量
INTERVAL_CHOICES = (1, 2, 3, 5, 10, 30)
COMPACT_W, COMPACT_H = 248, 124    # 紧凑态尺寸
EXPANDED_W = 306                   # 展开态宽度
```

**Q：能打包成 exe 发给没装 Python 的人吗？**

```bat
pip install pyinstaller
pyinstaller -F -w -n 鳄鱼派-沪深300基差 鳄鱼派_沪深300基差.py
```
生成的 `dist\鳄鱼派-沪深300基差.exe` 可直接双击运行。

## 更新日志

### v1.0.0 (2026-10-27)

首个公开版本：

- 紧凑态基差显示（点数 / 基差率 / 升贴水方向）+ 点击展开详情
- 持仓量加权口径，自动换月、自动过滤到期合约
- `WS_EX_TOOLWINDOW` 实现 Win+D 不被最小化、不进任务栏 / Alt+Tab
- 右键菜单：刷新间隔、置顶、开机自启、复位、退出
- 位置 / 展开状态 / 刷新间隔持久化，单实例，pythonw 无控制台启动

## 依赖

- Python 3.8+（标准库 `tkinter`、`ctypes`、`threading`、`json`）
- [`requests`](https://pypi.org/project/requests/)

```bat
pip install -r requirements.txt
```

## 免责声明

本项目仅为个人看盘便利工具。行情数据来自第三方免费接口，可能延迟、缺失或出错；基差计算口径为作者个人对齐文华终端的结果，不保证与任何商业终端完全一致。文中关于基差与市场情绪的描述属于经验性解读，**不构成任何投资建议，据此交易风险自负。**

## 关于

由 **鳄鱼派** 制作与维护，欢迎提 Issue / PR。

## License

[MIT](LICENSE)

---

## English

**Crocodile Pie (鳄鱼派) · CSI 300 Basis Widget**

A tiny always-on-top desktop widget for the **CSI 300 basis**: the spot index minus the open-interest-weighted IF futures price. It stays visible after **Win+D** (Show Desktop) because the window carries the `WS_EX_TOOLWINDOW` extended style — the same trick AHK scripts use (`winset, ExStyle, ^0x80`), implemented natively with `ctypes` (no AutoHotkey needed).

- Compact mode (248×124 px) shows only the basis number, its percentage and the premium direction.
- Click `▾` (or double-click) to expand: spot OHLC, weighted futures price, and every listed IF contract with price / change / open interest / weight.
- Auto contract rollover, right-click menu (refresh interval, always-on-top, autostart, quit), position & state persistence, single instance, no console window.

**What the number means**: a widening discount (basis falling) means hedgers are aggressively shorting futures; a **narrowing discount (basis rising) means hedging pressure is fading** — institutions are no longer uniformly bearish, and risk appetite usually recovers from the lows. Whether the index rises or falls, a converging basis is generally a good sign.

**Requirements**: Windows 10/11, Python 3.8+, `pip install requests`.

```bat
python 鳄鱼派_沪深300基差.py
```

Data comes from Sina Finance's free quotes endpoint (`hq.sinajs.cn`). For personal use; not investment advice. MIT licensed.
