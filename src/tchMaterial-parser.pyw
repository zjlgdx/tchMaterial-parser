# -*- coding: utf-8 -*-
# 国家中小学智慧教育平台 资源下载工具
# 项目地址：https://github.com/happycola233/tchMaterial-parser
# 作者：肥宅水水呀（https://space.bilibili.com/324042405）以及其他为本工具作出贡献的用户

# 本文件只是入口：Windows 上 .pyw 后缀决定双击运行不弹控制台窗口。
# 路径按本文件的位置解析，不依赖当前工作目录——冻结后 cwd 通常不是程序所在目录。
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from tchmaterial_parser.ui.app import main # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
