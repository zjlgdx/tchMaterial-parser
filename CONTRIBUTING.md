# 贡献者指南

感谢您愿意帮助改进本项目。这个项目面向**真实用户**，请尽量让每一次提交都保持清晰、可验证，并尊重国家中小学智慧教育平台及相关权利人的资源版权。

## 开始之前

- 提交问题前请**搜索**已有 [Issue](../../issues) 和 [Pull Request](../../pulls)，避免重复讨论。
- 发现程序无法运行、下载失败或界面异常时，请提交 [**🐛 Bug 报告**](../../issues/new?template=bug_report.yml)，并按模板补全相关信息，最好**附上错误信息截图**，以方便定位问题。
- 有新功能或改进建议时，请提交[**✨ 功能建议**](../../issues/new?template=feature_request.yml)，先说明您遇到的实际问题，再描述期望做法，这样更容易判断改动是否适合放进本项目。
- **不要**在 Issue、Pull Request、提交信息或代码等地方公开 Access Token。

## 本地开发

本项目使用 **Python 3.10 或更高版本**（`X | Y` 形式的类型注解仅在该版本及以后的版本可用）。

```sh
# 克隆项目
git clone https://github.com/happycola233/tchMaterial-parser.git
cd tchMaterial-parser

# 安装依赖
python -m pip install .

# 启动应用
python ./src/main.py
```

> [!NOTE]
> 本工具使用 **Tkinter** 构建图形界面。Windows 与 macOS 的官方 Python 通常已自带，而部分 Linux 发行版需要单独安装，例如在 Debian/Ubuntu 上执行 `sudo apt install python3-tk`。
>
> 此外，精简安装的 Linux 系统可能缺少中文字体与 Emoji 字体，此时界面上可能会出现方框等异常现象。可按需安装，例如在 Debian/Ubuntu 上执行 `sudo apt install fonts-noto-cjk fonts-noto-color-emoji`。

## 测试与检查

提交 Pull Request 前，请进行测试与检查：

```sh
# 安装开发用依赖
python -m pip install pytest flake8

# 运行测试与检查
python -m pytest
python -m flake8 . --count --select=E9,F63,F7,F82 --show-source --statistics
```

当前 CI 的静态检查只拦截语法错误、未定义变量等确定性问题，不把代码风格与中文注释行长等作为强制门槛。

格式化时请遵循仓库现有风格，如使用 **4 空格**缩进、**`snake_case`** 命名约定、字符串默认使用**双引号 `"`**、**不出现尾随空格**等。

## 打包验证

修改打包配置、资源文件或程序入口时，请额外验证 PyInstaller 构建：

```sh
python -m pip install pyinstaller
pyinstaller ./tchMaterial-parser.spec
```

构建产物位于 `dist` 目录。

## 代码与文案约定

- 本工具在设计上支持 Windows、Linux、macOS 操作系统，编写代码时应确保**跨平台兼容性**。
- 命名要**准确表达意图**；重命名变量、函数或模块时，请**同步更新**相关引用。
- 意图不明显的业务逻辑可以添加**简洁中文注释**；能从代码本身读懂的内容一般不需要注释。
- 不要为了理论上不可能发生的内部状态添加复杂兜底逻辑，校验应主要放在用户输入、文件系统、网络请求、外部 API 等**系统边界**。
- 涉及 UI 的改动，需要同时检查**浅色模式与深色模式**。
- 界面文案应直接面向**用户**，避免出现描述需求、规则或适用条件本身的元语言。
- 修改代码后，确保代码中**未出现明文 Access Token 等敏感信息**，且已执行**必要的测试与检查**。
