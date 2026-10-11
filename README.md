# Telegram TUI

一个使用 **Python + Textual + Telethon** 构建的 Telegram 终端客户端，可在本地终端或 VPS 的 SSH 会话中运行。

支持个人账号登录、会话搜索、历史消息、文字收发和实时更新。内置离线演示，无需 Telegram 账号即可体验界面。

![Telegram TUI 演示界面](docs/preview.png)

## 安装与启动

需要 Python 3.11+ 和 Git。以下命令适用于 Linux / macOS 的 shell；建议使用支持 UTF-8 / 真彩色的终端，并给窗口留出至少 100 × 30 字符的空间。

```bash
git clone https://github.com/12202106/telegram-tui-linux.git
cd telegram-tui-linux

python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .

# 先体验界面，无需 API 凭据，不访问 Telegram
telegram-tui --demo

# 登录真实账号
telegram-tui
```

也可以使用 `python -m telegram_tui` 启动。每次打开新终端后，先进入项目目录并运行 `source .venv/bin/activate`。

## 登录真实账号

1. 在 [Telegram 应用管理页面](https://my.telegram.org/apps) 创建应用，获取自己的 `api_id` 和 `api_hash`。本项目使用个人账号 API 凭据，不使用 BotFather 的机器人 Token。
2. 运行 `telegram-tui`，在设置界面填写 API 凭据。也可以提前设置下面的环境变量。
3. 输入带国际区号的手机号，再输入 Telegram 下发的验证码；账号启用了两步验证时，继续输入密码。验证码通常发送到你已登录的 Telegram 客户端，具体方式由 Telegram 决定。
4. 登录成功后保留本地会话文件，后续启动通常无需重新输入验证码。

```bash
# 下面都是占位值，请在自己的终端替换；不要将真实凭据发到聊天里。
export TELEGRAM_API_ID='123456'
export TELEGRAM_API_HASH='0123456789abcdef0123456789abcdef'
telegram-tui
```

界面输入的 API 凭据只保留在当前进程中，程序不会将它们写入配置文件。环境变量可在同一个 shell 中反复使用；新开终端或 SSH 会话后，需要重新导出，或从自己管理的私有 shell 配置中加载。即使已有登录会话，启动时仍需提供 API 凭据。

[`.env.example`](.env.example) 提供变量示例，程序**不会自动加载 `.env` 文件**。仅提供一项有效 API 凭据时，设置界面会补齐另一项；格式错误的环境变量会在启动时给出提示。

### Telegram 应用申请示例

在申请页面中，可以按下面的用途描述填写。名称可按需修改；遇到名称冲突时换一个短名称。

| 字段 | 示例 |
| --- | --- |
| App title | `VPS Terminal Chat` |
| Short name | `vpstermchat` |
| URL | 页面允许时留空，或填写自己的网站地址 |
| Platform | `Other` |
| Description | `Personal Telegram terminal client for use over SSH on my VPS.` |

提交后，在该页面保存好生成的 `api_id` 和 `api_hash`。应用申请和登录规则由 Telegram 决定，详见 [Telegram 官方说明](https://core.telegram.org/api/obtaining_api_id)。

## 在 VPS 上通过 SSH 使用

先使用自己的服务器地址登录：

```bash
ssh your-user@your-vps
```

在 VPS 中执行上面的安装命令，再启动 `telegram-tui`。手机号、验证码和两步验证密码都在当前终端的登录界面中输入；VPS 需要能够连接 Telegram。

后续连接同一台 VPS 时，可直接复用该用户目录下的会话文件：

```bash
cd telegram-tui-linux
source .venv/bin/activate
telegram-tui
```

这是一个需要交互式终端的 TUI，适合在 SSH 会话中前台运行。首次登录也需要终端交互，不能直接作为无终端的 systemd 服务完成登录。关闭 SSH 会话可能结束进程；重新启动后，登录状态通常可通过本地会话文件恢复。

## 使用方式

左侧选择或搜索会话，右侧阅读消息，在底部输入框编辑单行文字并按 Enter 发送。点击会话也可以切换。**打开真实会话会将它标记为已读**；当前会话收到的新消息也会同步已读状态。

| 按键 | 功能 |
| --- | --- |
| `Ctrl+Q` | 退出 |
| `Ctrl+K` | 聚焦会话搜索 |
| `Ctrl+N` / `Ctrl+P` | 下一个 / 上一个会话 |
| `Ctrl+R` | 刷新会话与消息 |
| `Ctrl+L` | 加载更早的消息 |
| `Esc` | 返回消息输入框 |
| `F1` | 帮助 |
| `Enter` | 在消息输入框发送文字 |
| `Tab` / `Shift+Tab` | 切换控件焦点 |

`--demo` 使用内置的虚构会话和消息，可体验搜索、历史记录和模拟收发。它不连接 Telegram，不读取真实账号凭据，消息也不会跨进程保存。

## 代理与会话位置

支持 SOCKS5 和 HTTP 代理，可通过环境变量或命令行设置：

```bash
export TELEGRAM_PROXY='socks5://127.0.0.1:1080'
telegram-tui

# 命令行参数优先于对应环境变量
telegram-tui --proxy 'http://127.0.0.1:8080'
telegram-tui --session "$HOME/.local/share/telegram-tui/second-account.session"
```

带认证的代理格式为 `socks5://username:password@host:port` 或 `http://username:password@host:port`，用户名和密码中的 `@`、`:` 等特殊字符需要 URL 编码。IPv6 地址请使用方括号，例如 `socks5://[::1]:1080`。代理必须显式配置；程序不会将 `HTTP_PROXY` / `HTTPS_PROXY` 自动作为 Telegram 代理。

在 VPS 上运行时，`127.0.0.1` 指的是 **VPS 本身**，代理应在 VPS 对应端口监听；电脑上运行的本地代理不会自动被 VPS 使用。能够直接连接 Telegram 时，无需设置代理。

| 环境变量 | 说明 |
| --- | --- |
| `TELEGRAM_API_ID` | Telegram 应用的正整数 ID |
| `TELEGRAM_API_HASH` | Telegram 应用的 32 位十六进制 Hash |
| `TELEGRAM_SESSION` | 可选，会话文件路径 |
| `TELEGRAM_PROXY` | 可选，SOCKS5 / HTTP 代理 URL |
| `XDG_DATA_HOME` | 可选，默认会话文件的数据目录根路径 |

默认会话文件位于 `~/.local/share/telegram-tui/account.session`；设置了有效的绝对路径 `XDG_DATA_HOME` 时，改为 `$XDG_DATA_HOME/telegram-tui/account.session`。同一个会话文件不要同时用于多个运行中的进程。

**会话文件代表账号登录权限。** 不要分享、提交到 Git 或存放到公开同步目录。在支持 POSIX 权限的平台上，程序将会话文件设为 `0600`，新建的会话存储目录设为 `0700`；自定义路径时也应选择只有自己能访问的目录。仓库的 [`.gitignore`](.gitignore) 已排除会话文件和本地环境配置。

退出程序只断开连接，不会注销账号。需要撤销本客户端登录时，在 Telegram 官方客户端的「设置 → 设备」中终止对应会话，再删除本地 `.session` 文件。

## 当前范围

- 支持个人账号、已有私聊 / 群组 / 频道的会话列表，以及有发言权限会话的文字消息。
- 历史消息按批次加载；左侧搜索筛选已加载的会话，不是全网联系人或全文消息搜索。
- 图片、文件、贴纸等媒体显示文字占位提示，暂不支持预览、上传和下载。
- 暂不支持创建会话、回复 / 编辑 / 删除消息、反应、语音通话或秘密聊天。
- 网络错误、权限不足、验证码错误和 Telegram 限流会显示提示；连接失败后可按界面提示重试。

真实账号登录和消息收发需要用户自己的 Telegram API 凭据与可用网络。目前的验证基于离线演示和模拟后端，尚未使用真实 Telegram 账号完成端到端验证。

## 开发验证

```bash
# 在已激活的虚拟环境中安装开发依赖
python -m pip install -e '.[dev]'
python -m pytest
python -m ruff check .
telegram-tui --version
```

源码位于 [`src/telegram_tui/`](src/telegram_tui/)，自动化测试位于 [`tests/`](tests/)。界面与后端通过独立的数据模型和异步接口交互，测试不访问真实 Telegram 账号。

## 共同开发

- [12202106](https://github.com/12202106) — 需求提出与项目维护。
- [OpenAI Codex](https://developers.openai.com/codex/) — 协助代码实现、离线测试和文档编写。

## 许可证

本项目使用 [MIT License](LICENSE)。

## 参考文档

参考：[Telegram API 凭据申请](https://core.telegram.org/api/obtaining_api_id)、[Telethon 登录与代理](https://docs.telethon.dev/en/stable/basic/signing-in.html)、[Telethon 会话文件](https://docs.telethon.dev/en/stable/concepts/sessions.html)、[Textual 文档](https://textual.textualize.io/)。
