# [国家中小学智慧教育平台 电子课本](https://basic.smartedu.cn/tchMaterial/)下载工具

![Python Version](https://img.shields.io/badge/Python-3.10%2B-blue.svg)
![License](https://img.shields.io/badge/License-MIT-green.svg)
![Made With Love❤️](https://img.shields.io/badge/Made_With-%E2%9D%A4-red.svg)

> [!TIP]
> 🚀本工具在未设置 Access Token 时也可下载电子课本，欢迎体验！

本工具可以帮助您从[**国家中小学智慧教育平台**](https://basic.smartedu.cn/)获取电子课本的 PDF 文件网址并进行下载，让您更方便地获取课本内容。

## ✨工具特点

- 🔑**支持 Access Token 登录**：支持用户手动输入 Access Token，Windows 下存入注册表，macOS 与 Linux 下存入用户配置目录（文件权限 `0600`），下次启动可自动加载。
- 📚**支持批量下载**：一次输入多个电子课本预览页面网址，即可批量下载 PDF 课本文件。
- 📂**自动文件命名**：自动使用教材名称作为文件名，并清洗文件系统不接受的字符；同名教材依次加 `(2)`、`(3)` 后缀，不会互相覆盖。
- 🖥️**高 DPI 适配**：优化 UI 以适配高分辨率屏幕，避免界面模糊问题。
- 📊**下载进度可视化**：实时显示总体下载进度与已完成数量。
- 💻**跨平台支持**：支持 Windows、Linux、macOS 等操作系统（需要图形界面）。

![程序截图](./res/PixPin_2025-03-14_23-44-26.png)

## 📥下载与安装方法

### GitHub Releases 页面

由于我们的精力有限，本项目的 [GitHub Releases 页面](https://github.com/happycola233/tchMaterial-parser/releases)**仅会发布适用于 Windows 与 Linux 操作系统的 x64 架构**的程序。

在下载完成之后，即可运行本程序，不需要额外的安装步骤。

### 从源码运行

需要 **Python 3.10 或更高版本**（Windows、macOS、Linux 均可）：

```sh
pip install -r requirements.txt
python src/tchMaterial-parser.pyw
```

`pywin32` 带有平台标记，只会在 Windows 上安装。Linux 上如果提示缺少图形库，请先安装系统的 `python3-tk` 包。

### Arch 用户软件仓库（AUR）

对于 **Arch Linux** 操作系统，本程序已发布至[Arch 用户软件仓库](https://aur.archlinux.org/packages/tchmaterial-parser)，因此您还可以通过在终端中输入以下命令安装：

```sh
yay -S tchmaterial-parser
```

感谢 [@iamzhz](https://github.com/iamzhz) 为本工具制作了发行包（[#26](../../issues/26)）！

## 🛠️使用方法

### 1. 输入教材链接⌨️

将电子课本的**预览页面网址**粘贴到程序文本框中，支持多个 URL（每行一个）。

也可以在下方的**教材目录**里逐层展开，双击教材即可把它的网址加入上方文本框；目录上方的搜索框支持按教材名直接搜索。

**示例网址**：

```text
https://basic.smartedu.cn/tchMaterial/detail?contentType=assets_document&contentId=XXXXXX&catalogType=tchMaterial&subCatalog=tchMaterial
```

### 2. 设置 Access Token🔑

> [!TIP]
> 这一步操作**不再必要**：未设置 Access Token 时程序会使用其他方法下载资源。然而，这一方法**并不长期有效，且对于部分资源无效**，因此仍然建议您进行这一步操作。

1. **打开浏览器**，访问[国家中小学智慧教育平台](https://auth.smartedu.cn/uias/login)并**登录账号**。
2. 按下 **F12** 或 **Ctrl+Shift+I**，或右键——检查（审查元素）打开**开发者工具**，选择**控制台（Console）**。
3. 在控制台粘贴以下代码后回车（Enter）：

   ```js
   (function() {
     const authKey = Object.keys(localStorage).find(key => key.startsWith("ND_UC_AUTH"));
     if (!authKey) {
       console.error("未找到 Access Token，请确保已登录！");
       return;
     }
     const tokenData = JSON.parse(localStorage.getItem(authKey));
     const accessToken = JSON.parse(tokenData.value).access_token;
     console.log("%cAccess Token:", "color: green; font-weight: bold", accessToken);
   })();
   ```
  
4. 复制控制台输出的 **Access Token**，然后在本程序中点击 “**设置 Token**” 按钮，粘贴并保存 Token。

> [!NOTE]
> Access Token 可能会过期，若下载失败提示 **401 Unauthorized** 或 **403 Forbidden**，请重新获取并设置新的 Token。

### 3. 开始下载🚀

点击 “**下载**” 按钮，程序将自动解析并下载 PDF 课本。

本工具支持**批量下载**，所有 PDF 文件会自动按课本名称命名并保存在选定目录中。

## ❓常见问题

### 1. 为什么下载失败？⚠️

- 如果您没有设置 Access Token，可能是本程序使用的方法失效了，请[**设置 Access Token**](#2-设置-access-token)🔑。
- 如果您设置了 Access Token，由于其具有时效性（一般为 7 天），因此极有可能是 Access Token 过期了，请重新获取新的 Access Token。
- **确认网络连接是否正常**🌐，有时网络不稳定可能导致下载失败。
- **确保输入的网址有效**🔗，部分旧资源可能已被移除。

### 2. Access Token 保存在哪里？💾

- **Windows 操作系统**：Token 会存储在**注册表** `HKEY_CURRENT_USER\Software\tchMaterial-parser` 项中的 `AccessToken` 值。
- **macOS 操作系统**：Token 会存储在**文件** `~/Library/Application Support/tchMaterial-parser/data.json` 中。
- **Linux 操作系统**：Token 会存储在**文件** `$XDG_CONFIG_HOME/tchMaterial-parser/data.json`（默认即 `~/.config/tchMaterial-parser/data.json`）中。

这些文件的权限会被设为 `0600`，同一台机器上的其他用户无法读取。

### 3. Token 会不会泄露？🔐

- 本程序**不会上传** Token，也不会存储在云端，仅用于本地请求授权。
- **请勿在公开场合分享 Token**，以免您的账号被他人使用，造成严重后果。

## ⭐Star History

[![Star History Chart](https://api.star-history.com/svg?repos=happycola233/tchMaterial-parser&type=Date)](https://star-history.com/#happycola233/tchMaterial-parser&Date)

## 🤝贡献指南

如果您发现 Bug 或有改进建议，欢迎提交 **Issue** 或 **Pull Request**，让我们一起完善本工具！

本项目的设计文档位于 [`docs/designs/`](docs/designs/)。开发时请先安装测试依赖并跑一遍测试：

```sh
pip install -r requirements.txt -r requirements-dev.txt
pytest
flake8 . --select=E9,F63,F7,F82
```

## 📜许可证

本项目基于 [MIT 许可证](LICENSE)，欢迎自由使用和二次开发。

## 💌友情链接

- 📚您也可以在 [ChinaTextbook](https://github.com/TapXWorld/ChinaTextbook) 项目中下载归档的教材 PDF。
