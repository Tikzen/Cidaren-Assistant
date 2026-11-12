## Windows x64 便携预览版

本次提供词达人小助手的首个 Windows x64 EXE 构建，包含新版桌面界面、自定义任务队列、自动答题、重做提分和内置 Token 获取组件。

### 下载与启动

1. 下载附件 **`CidarenAssistant-Windows-x64.zip`**，无需安装 Python。
2. 解压整个压缩包到可写目录。
3. 打开解压后的文件夹，双击 **`CidarenAssistant.exe`**。
4. 登录后添加任务、调整队列顺序，再开始执行。

请保留同目录下的资源、依赖文件和 `mitmdump.exe`；后者由内置 Token 获取工具自动调用，不需要单独打开。不要在压缩包内部直接运行 EXE。

- [使用教程](https://github.com/Tikzen/Cidaren-Assistant/blob/main/docs/使用教程.md)
- `SHA256SUMS.txt` 提供附件的 SHA-256 校验值。

### 验证情况

- 在 GitHub Windows runner 上完成原生 x64 构建。
- **151 项离线回归测试通过**。
- 冻结后的 EXE 通过界面创建、本地语言模型加载、独立再次启动及 Windows 原生窗口启动检查。
- 内置 `mitmdump.exe --version` 检查通过。
- 验证未登录真实账号，未修改系统代理；真实微信抓取与在线答题仍需使用者验收。

本包是**未签名的便携预览版**，Windows 可能显示未知发布者提示。此版本适用于 Windows x64，不能在 macOS 上直接运行。

构建提交：`0012df09f6569d638e16ed363997ace716cc34d7`。本次打包改动位于独立的 `codex/windows-exe-build` 分支，未改动主分支。
