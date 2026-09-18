# -*- coding: utf-8 -*-
# 左侧资源列表：勾选教材或分类、搜索筛选、封面按需加载与悬停预览

import io
import logging
import time
import tkinter as tk
from collections import OrderedDict
from collections.abc import Callable, Iterator
from tkinter import ttk
import tkinter.font as tkfont
from PIL import Image, ImageDraw, ImageOps, ImageTk

from . import runtime, theme
from .runtime import scaled, thread_it, ui_call
from .widgets import auto_hide_scrollbar, bind_context_menu
from ..catalog import count_resource_items, filter_resource_items
from ..images import fit_cover_image
from ..logging_utils import log_duration
from ..network import session
from ..platform_utils import os_name, print_error

logger = logging.getLogger(__name__)

def build_resource_url(item_path: str, resource_data: dict) -> str: # 根据树项路径与资源数据生成资源页面链接
    resource_type = resource_data.get("resource_type_code") or "assets_document"
    content_id = resource_data.get("content_id") or item_path.split(":")[-1]
    root_id = item_path.split(":")[0]
    if resource_type == "teachingmaterials":
        return f"https://basic.smartedu.cn/syncClassroom{'/prepare' if root_id == '__internal_prepare_lesson' else ''}?defaultTag={'%2F'.join(item_path.split(':')[1:])}"
    return f"https://basic.smartedu.cn/tchMaterial/detail?contentType={resource_type}&contentId={content_id}&catalogType=tchMaterial&subCatalog=tchMaterial"

def iter_leaf_resources(items: dict[str, dict], parent_path: str = "") -> Iterator[tuple[str, dict]]: # 遍历分类子树，产出每个末级资源的（树项路径， 资源数据）
    for option_id, option_data in items.items():
        item_path = f"{parent_path}:{option_id}" if parent_path else option_id
        children: dict[str, dict] = option_data.get("children", {})
        if children: # 分类节点继续向下遍历
            yield from iter_leaf_resources(children, item_path)
        else:
            yield item_path, option_data

def collect_resource_urls(items: dict[str, dict], parent_path: str = "") -> list[str]: # 递归收集分类子树中所有末级资源的链接
    return [build_resource_url(item_path, resource_data) for item_path, resource_data in iter_leaf_resources(items, parent_path)]

def find_tree_node(items: dict[str, dict], item_path: str) -> dict | None: # 按树项路径在分类树中定位节点数据
    node = None
    branch = items
    for segment in item_path.split(":"):
        node = branch.get(segment)
        if node is None:
            return None
        branch = node.get("children", {})
    return node

def category_check_state(leaf_ids: list[str], checked_items: set[str]) -> str: # 依据子树内末级资源的勾选情况得出分类的三态
    if not leaf_ids:
        return "unchecked"
    checked_count = sum(1 for leaf_id in leaf_ids if leaf_id in checked_items)
    if checked_count == 0:
        return "unchecked"
    return "checked" if checked_count == len(leaf_ids) else "partial"

def should_check_category(leaf_ids: list[str], checked_items: set[str]) -> bool: # 点击分类时的目标状态：未全选时补全勾选，已全选时取消
    return category_check_state(leaf_ids, checked_items) != "checked"

def draw_checkbox_image(size: int, state: str, colors: dict[str, str]) -> Image.Image: # 绘制跟随主题配色的三态复选框图标
    # 放大绘制后缩回目标尺寸，让圆角、对勾与半选横线的边缘抗锯齿。
    scale = 4
    target_size = size
    border_width = max(2, size // 10) * scale
    corner_radius = max(2, size // 5) * scale
    check_width = max(2, size // 7) * scale
    size *= scale
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    selected = state in ("checked", "partial")
    draw.rounded_rectangle(
        (border_width // 2, border_width // 2, size - 1 - border_width // 2, size - 1 - border_width // 2),
        radius=corner_radius,
        fill=colors["selbg"] if state == "checked" else colors["surface"],
        outline=colors["selbg"] if selected else colors["muted"],
        width=border_width,
    )
    if state == "checked": # 对勾
        draw.line(
            (size * 0.24, size * 0.53, size * 0.44, size * 0.74, size * 0.78, size * 0.3),
            fill=colors["selfg"],
            width=check_width,
            joint="curve",
        )
    elif state == "partial": # 半选横线
        inset = size * 0.32
        draw.line((inset, size / 2, size - inset, size / 2), fill=colors["selbg"], width=border_width)
    return image.resize((target_size, target_size), Image.Resampling.LANCZOS)

STATUS_ITEM_ID = "__internal_status" # 资源目录尚未就绪时，树视图中提示行的树项 ID
PLACEHOLDER_SUFFIX = ":__internal_placeholder" # 占位子项的树项 ID 后缀；Tk 只给有子项的行画展开箭头
SCAN_DEBOUNCE_MS = 80 # 滚动停下多久后扫描一次可见行
SCAN_MAX_WAIT_MS = 250 # 连续滚动时两次扫描的最长间隔，避免去抖一直被推迟
VISIBLE_SCAN_PROBE_STEP = 2 # 扫描起点的试探步长，用于跨过树视图上边框
PREVIEW_CACHE_SIZE = 200 # 悬停预览缓存的封面张数
COVER_WORKERS = 4 # 同时下载封面的线程数
CATALOG_TICK_MS = 1000 # 加载期间刷新提示行的间隔
CATALOG_SLOW_SECONDS = 45 # 加载超过这么久就补一句可操作的建议；单次请求的读超时是 60 秒
CATALOG_SLOW_HINT = "，网络较慢，可先在右侧手动填写资源链接下载"
# 以下阈值只用于耗时日志；滚动与可见行刷新是每帧热路径，不在其中记日志
TREE_REBUILD_SLOW_MS = 300 # 搜索后重建树视图
TREE_EXPAND_SLOW_MS = 100 # 首次展开一个分类
CHECK_SYNC_SLOW_MS = 100 # 勾选状态与 URL 输入框同步

def visible_tree_rows(treeview: ttk.Treeview) -> list[str]: # 逐行取出当前屏幕上的树项
    if not treeview.get_children():
        return []

    rows: list[str] = []
    height = treeview.winfo_height()
    y = 0
    while y < height:
        item_id = treeview.identify_row(y)
        # 上边框那几个像素要么取不到行，要么取到视口上方的行（其 bbox 为空），两种都算未命中
        box = treeview.bbox(item_id) if item_id else None
        if not box:
            if rows:
                break
            y += VISIBLE_SCAN_PROBE_STEP
            continue
        rows.append(item_id)
        y = box[1] + box[3] + 1
    return rows

def build_resource_tree(
    pane: ttk.Frame, resource_list: dict[str, dict], url_text: tk.Text, status: str = "",
) -> Callable[..., None]: # 在给定的子框架内构建资源列表；status 非空表示资源目录尚未就绪，先用提示行占位，返回的函数用于随后填入资源目录
    pane.columnconfigure(0, weight=1)
    pane.rowconfigure(2, weight=1)

    treeview_header = ttk.Frame(pane)
    treeview_header.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, scaled(6)))
    treeview_header.columnconfigure(1, weight=1)
    treeview_label = ttk.Label(treeview_header, text="资源列表", style="Heading.TLabel") # 添加树视图标签
    treeview_label.grid(row=0, column=0, sticky="w")
    checked_count_label = ttk.Label(treeview_header, style="Caption.TLabel") # 显示当前勾选的教材数量
    checked_count_label.grid(row=0, column=1, sticky="e", padx=(0, scaled(8)))
    search_status_label = ttk.Label(treeview_header, style="Caption.TLabel")
    search_status_label.grid(row=0, column=2, sticky="e")

    search_frame = ttk.Frame(pane)
    search_frame.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(0, scaled(8)))
    search_frame.columnconfigure(1, weight=1)
    search_label = ttk.Label(search_frame, text="搜索")
    search_label.grid(row=0, column=0, padx=(0, scaled(8)))
    search_var = tk.StringVar()
    search_entry = ttk.Entry(search_frame, textvariable=search_var, font="AppBodyFont")
    search_entry.grid(row=0, column=1, sticky="ew")
    bind_context_menu(search_entry, "noundo")

    clear_search_btn = ttk.Button(search_frame, text="清除", width=5, command=lambda: search_var.set(""))
    clear_search_btn.grid(row=0, column=2, padx=(scaled(6), 0))

    treeview = ttk.Treeview(pane, style="Custom.Treeview", show="tree", selectmode="extended", height=12) # 创建树视图，使用自定义样式（该样式在 apply_theme() 中配置），隐藏列标题；勾选状态用复选框图标表达，选择模式仅供键盘导航
    treeview.column("#0", stretch=False)
    treeview.grid(row=2, column=0, sticky="nsew")
    treeview_scrollbar = ttk.Scrollbar(pane, orient="vertical", command=treeview.yview)
    treeview_scrollbar.grid(row=2, column=1, sticky="ns")
    treeview_horizontal_scrollbar = ttk.Scrollbar(pane, orient="horizontal", command=treeview.xview)
    treeview.configure(xscrollcommand=lambda f, l: auto_hide_scrollbar(treeview_horizontal_scrollbar, f, l))
    treeview_horizontal_scrollbar.grid(row=3, column=0, sticky="ew")

    tree_item_data: dict[str, dict] = {} # 键为树项 ID，值为资源数据
    tree_item_paths: dict[str, tuple[str, ...]] = {} # 保存完整分类路径，用于悬停提示
    item_icon_generation: dict[str, int] = {} # 各树项图标合成时的代际，与当前代际不符即为待刷新
    tree_item_images: dict[str, ImageTk.PhotoImage] = {} # 持有树项图标（复选框与封面的合成图）的引用防止被回收，筛选后继续复用
    tree_cover_images: dict[str, Image.Image] = {} # 已加载封面的缩放图，勾选状态变化时与复选框重新合成
    preview_cover_pils: OrderedDict[str, Image.Image] = OrderedDict() # 悬停预览用的大尺寸封面，按最近使用保留
    loading_tree_images: set[str] = set() # 正在下载的封面，与 pending_covers 一样只在主线程改动
    failed_tree_images: set[str] = set() # 下载失败的封面，本次运行不再重复请求
    pending_covers: list[tuple[str, str]] = [] # 最近一次可见扫描得出的待载封面（树项 ID 与封面地址）
    checked_items: set[str] = set() # 已勾选末级资源的树项路径，搜索重建树视图后仍保留
    leaf_urls = {item_id: build_resource_url(item_id, data) for item_id, data in iter_leaf_resources(resource_list)}
    catalog_status = status # 资源目录就绪后置空，此前树视图中只显示一行提示
    catalog_loading = bool(status) # 提示行分两种：加载中的阶段提示会带上已用时间，终态（就绪或失败）的文案原样保留
    catalog_started_at = time.monotonic()
    catalog_tick_after_id: str | None = None
    checkbox_pils: dict[str, Image.Image] = {} # 三态复选框底图，跟随主题配色重建
    checkbox_icons: dict[str, ImageTk.PhotoImage] = {} # 无封面树项直接使用的复选框图标（已含右侧间距）
    tree_font = tkfont.nametofont("AppBodyFont")
    tree_cover_size = (scaled(26), scaled(28))
    tree_cover_gap = scaled(8) # 用透明区域拉开封面与标题，避免改变封面尺寸
    checkbox_size = scaled(18)
    checkbox_gap = scaled(6) # 复选框与封面、标题之间的间距
    preview_cover_size = (scaled(80), scaled(112))
    tree_content_width = 0
    icon_generation = 0 # 勾选或配色变化后自增，屏幕外的树项据此标脏

    def get_tree_cover_gap(display_name: str) -> int: # 名称以中文左括号开头时不添加封面与标题间隔
        return 0 if display_name.startswith("（") else tree_cover_gap

    def catalog_status_text() -> str: # 加载中的提示带上已用时间，久了再补一句建议；终态文案原样返回
        if not catalog_loading:
            return catalog_status

        elapsed = int(time.monotonic() - catalog_started_at)
        if elapsed < 1:
            return catalog_status
        return f"{catalog_status}（已用 {elapsed} 秒{CATALOG_SLOW_HINT if elapsed >= CATALOG_SLOW_SECONDS else ''}）"

    def update_catalog_status_row() -> None: # 只改提示行的文字，不重建整棵树
        if treeview.exists(STATUS_ITEM_ID):
            treeview.item(STATUS_ITEM_ID, text=catalog_status_text())

    def cancel_catalog_tick() -> None:
        nonlocal catalog_tick_after_id
        if catalog_tick_after_id:
            runtime.root.after_cancel(catalog_tick_after_id)
            catalog_tick_after_id = None

    def schedule_catalog_tick() -> None: # 加载期间每秒刷新一次提示行，让「还在加载」与「卡死」看得出区别
        nonlocal catalog_tick_after_id
        cancel_catalog_tick()
        if catalog_loading:
            catalog_tick_after_id = runtime.root.after(CATALOG_TICK_MS, on_catalog_tick)

    def on_catalog_tick() -> None:
        nonlocal catalog_tick_after_id
        catalog_tick_after_id = None
        if not catalog_loading: # 已经是终态，不能再把秒数写回文案上
            return
        update_catalog_status_row()
        schedule_catalog_tick()

    def insert_tree_level(parent: str, items: dict[str, dict], parent_names: tuple[str, ...], expand_all: bool) -> None: # 插入一层树项
        nonlocal tree_content_width
        for option_id, option_data in items.items():
            item_id = f"{parent}:{option_id}" if parent else option_id
            display_name = option_data["display_name"]
            path_names = (*parent_names, display_name)
            tree_item_data[item_id] = option_data
            tree_item_paths[item_id] = path_names
            tree_item_images[item_id] = compose_item_image(item_id)
            open_item = expand_all or not parent
            treeview.insert(
                parent,
                "end",
                iid=item_id,
                text=display_name,
                image=tree_item_images[item_id],
                open=open_item,
            )
            children: dict[str, dict] = option_data.get("children", {})
            if children and open_item: # 展开的分类立即填充下一层
                insert_tree_level(item_id, children, path_names, expand_all)
            elif children: # 折叠的分类先挂占位子项，Tk 才会给它画展开箭头
                treeview.insert(item_id, "end", iid=f"{item_id}{PLACEHOLDER_SUFFIX}", text="")

            depth_width = len(path_names) * scaled(20)
            if option_data.get("custom_properties", {}).get("thumbnails"):
                image_width = checkbox_size + checkbox_gap + tree_cover_size[0] + get_tree_cover_gap(display_name) + scaled(4)
            else:
                image_width = checkbox_size + checkbox_gap
            tree_content_width = max(tree_content_width, depth_width + image_width + tree_font.measure(display_name) + scaled(20))

    def on_tree_open(_event: tk.Event) -> None: # 首次展开分类时才插入它的子项
        item_id = treeview.focus()
        node = tree_item_data.get(item_id)
        if node is None: # 资源目录尚未就绪时的提示行没有子项数据
            return

        children = treeview.get_children(item_id)
        # 方向键右在末级资源上也会发这个事件；已经填充过的分类，子项不再是占位项
        if len(children) != 1 or not children[0].endswith(PLACEHOLDER_SUFFIX):
            return

        with log_duration(logger, f"展开分类 {item_id}", TREE_EXPAND_SLOW_MS):
            treeview.delete(children[0])
            insert_tree_level(item_id, node["children"], tree_item_paths[item_id], expand_all=False)
            resize_tree_column(treeview.winfo_width()) # 新插入的项可能比现有内容更宽
        schedule_visible_refresh()

    def resize_tree_column(width: int) -> None: # 让树列至少铺满可视区域，内容过长时启用横向滚动
        treeview.column("#0", width=max(tree_content_width, width - scaled(2)))

    def rebuild_checkbox_images() -> None: # 按当前主题配色生成三态复选框图标
        checkbox_pils.clear()
        checkbox_icons.clear()
        for state in ("checked", "partial", "unchecked"):
            checkbox_pils[state] = draw_checkbox_image(checkbox_size, state, theme.current_colors)
            checkbox_icons[state] = ImageTk.PhotoImage(ImageOps.expand(checkbox_pils[state], border=(0, 0, checkbox_gap, 0), fill=(0, 0, 0, 0)))

    def invalidate_item_icons() -> None: # 标记全部树项图标过期，由可见行扫描按需重新合成
        nonlocal icon_generation
        icon_generation += 1

    def on_theme_changed() -> None: # 主题切换后重建复选框配色并刷新全部树项图标
        rebuild_checkbox_images()
        invalidate_item_icons()
        # 三态复选框是共享图片，换配色后旧图会被回收，已插入的树项都得当场换上新图
        for item_id in list(tree_item_data):
            refresh_item_image(item_id)

    def item_check_state(item_id: str) -> str: # 末级资源为勾选/未勾选两态，分类按后代整体勾选情况显示三态
        node = tree_item_data.get(item_id) or find_tree_node(resource_list, item_id)
        if node is None:
            return "unchecked"
        children = node.get("children")
        if children:
            leaf_ids = [leaf_id for leaf_id, _leaf_data in iter_leaf_resources(children, item_id)]
            return category_check_state(leaf_ids, checked_items)
        return "checked" if item_id in checked_items else "unchecked"

    def compose_item_image(item_id: str) -> ImageTk.PhotoImage: # 合成树项图标：勾选状态对应的复选框与已加载的封面
        item_icon_generation[item_id] = icon_generation # 合成即记账，供扫描判断该项是否还需要刷新
        state = item_check_state(item_id)
        cover = tree_cover_images.get(item_id)
        if cover is None: # 尚未加载封面的树项直接复用带间距的复选框图标
            return checkbox_icons[state]
        checkbox = checkbox_pils[state]
        height = max(checkbox.height, cover.height)
        image = Image.new("RGBA", (checkbox.width + checkbox_gap + cover.width, height), (0, 0, 0, 0))
        image.alpha_composite(checkbox, (0, (height - checkbox.height) // 2))
        image.alpha_composite(cover, (checkbox.width + checkbox_gap, (height - cover.height) // 2))
        return ImageTk.PhotoImage(image)

    def refresh_item_image(item_id: str) -> None: # 重新合成并应用树项图标（勾选状态或封面变化后调用）
        image = compose_item_image(item_id)
        tree_item_images[item_id] = image
        if treeview.exists(item_id):
            treeview.item(item_id, image=image)

    def remember_preview_cover(item_id: str, image: Image.Image) -> None: # 只留最近看过的若干张预览图，整个目录的大图不常驻
        preview_cover_pils[item_id] = image
        preview_cover_pils.move_to_end(item_id)
        while len(preview_cover_pils) > PREVIEW_CACHE_SIZE:
            preview_cover_pils.popitem(last=False)

    def reload_preview_cover(item_id: str) -> None: # 预览图已被淘汰时重新取一次，下次悬停即可显示
        if item_id in loading_tree_images or item_id in failed_tree_images:
            return
        thumbnails = (tree_item_data.get(item_id) or {}).get("custom_properties", {}).get("thumbnails")
        if not thumbnails:
            return

        # 用户正看着这一项，直接下载而不排队：排队会被下一次可见扫描的整体替换挤掉，名额占满时更是永远轮不到
        pending_covers[:] = [cover for cover in pending_covers if cover[0] != item_id]
        loading_tree_images.add(item_id)
        thread_it(load_tree_icon, item_id, thumbnails[0])

    def apply_tree_icon(item_id: str, image: Image.Image | None) -> None:
        loading_tree_images.discard(item_id)
        if image is None:
            failed_tree_images.add(item_id)
        else:
            tree_image = fit_cover_image(image, tree_cover_size)
            # 搜索重建后树项可能暂不在当前视图中，仍缓存封面供下次合成复用
            resource_data = tree_item_data.get(item_id) or find_tree_node(resource_list, item_id) or {}
            cover_gap = get_tree_cover_gap(resource_data.get("display_name", ""))
            if cover_gap:
                tree_image = ImageOps.expand(tree_image, border=(0, 0, cover_gap, 0), fill=(0, 0, 0, 0))
            tree_cover_images[item_id] = tree_image
            remember_preview_cover(item_id, image)
            item_icon_generation.pop(item_id, None) # 封面到了，图标要重新合成
            refresh_visible_items(False)
        pump_cover_queue() # 空出的并发名额立刻交给下一张封面

    def load_tree_icon(item_id: str, url: str) -> None: # 在线程中下载封面，在主线程中更新控件
        try:
            resp = session.get(url)
            if not resp.ok:
                logger.info("封面 %s 返回 %s，本次运行不再重试", item_id, resp.status_code)
                ui_call(apply_tree_icon, item_id, None)
                return
            image = fit_cover_image(Image.open(io.BytesIO(resp.content)), preview_cover_size)
            ui_call(apply_tree_icon, item_id, image)
        except Exception as e:
            print_error(e)
            ui_call(apply_tree_icon, item_id, None)

    def pump_cover_queue() -> None: # 在并发上限内把待载封面交给后台线程
        while pending_covers and len(loading_tree_images) < COVER_WORKERS:
            item_id, url = pending_covers.pop(0)
            loading_tree_images.add(item_id)
            thread_it(load_tree_icon, item_id, url)

    def queue_tree_icons(covers: list[tuple[str, str]]) -> None: # 待载封面始终取自最近一次可见扫描，滚出视野的项随之作废
        pending_covers[:] = [cover for cover in covers if cover[0] not in loading_tree_images]
        pump_cover_queue()

    scan_after_id: str | None = None
    deferred_since = 0.0 # 本轮推迟从何时开始，用来限制扫描最多被推迟多久

    def refresh_visible_items(queue_covers: bool) -> None: # 只处理屏幕上的行，开销与目录规模无关
        covers: list[tuple[str, str]] = []
        for item_id in visible_tree_rows(treeview):
            resource_data = tree_item_data.get(item_id) # 提示行与占位子项不是资源
            if resource_data is None:
                continue
            if item_icon_generation.get(item_id) != icon_generation:
                refresh_item_image(item_id)
            if not queue_covers or item_id in tree_cover_images or item_id in failed_tree_images:
                continue
            thumbnails = resource_data.get("custom_properties", {}).get("thumbnails")
            if thumbnails:
                covers.append((item_id, thumbnails[0]))
        if queue_covers:
            queue_tree_icons(covers)

    def run_cover_scan() -> None: # 推迟到期，扫一次可见行并重新开始计时
        nonlocal scan_after_id, deferred_since
        scan_after_id = None
        deferred_since = 0.0
        refresh_visible_items(True)

    def schedule_visible_refresh() -> None: # 滚动期间只保留一个待执行的扫描
        nonlocal scan_after_id, deferred_since
        now = time.monotonic()
        if scan_after_id is None: # 新一轮推迟从这一刻算起，手势的第一个事件不会当场扫描
            deferred_since = now
        else:
            runtime.root.after_cancel(scan_after_id)
            scan_after_id = None

        # 每次重排都缩短到距最长间隔的剩余时间，滚动不停时扫描也不会被一再推迟
        delay = min(SCAN_DEBOUNCE_MS, round(SCAN_MAX_WAIT_MS - (now - deferred_since) * 1000))
        if delay <= 0:
            run_cover_scan()
            return
        scan_after_id = runtime.root.after(delay, run_cover_scan)

    def on_tree_view_change(first: str, last: str) -> None:
        auto_hide_scrollbar(treeview_scrollbar, first, last)
        refresh_visible_items(False) # 滚进视野的行要立刻显示正确的勾选状态
        schedule_visible_refresh()

    def refresh_resource_tree() -> None: # 根据搜索词重建树视图
        nonlocal tree_content_width
        with log_duration(logger, "重建资源树", TREE_REBUILD_SLOW_MS):
            query = search_var.get().strip()
            visible_items = filter_resource_items(resource_list, query)

            leave_tree()
            treeview.delete(*treeview.get_children())
            tree_item_data.clear()
            tree_item_paths.clear()
            item_icon_generation.clear()
            tree_content_width = 0
            insert_tree_level("", visible_items, (), expand_all=bool(query))
            resize_tree_column(treeview.winfo_width())
            clear_search_btn.state(["!disabled"] if query else ["disabled"])

            if catalog_status: # 资源目录尚未就绪，用一行提示占位
                treeview.insert("", "end", iid=STATUS_ITEM_ID, text=catalog_status_text())
                search_status_label.config(text="")
                return

            result_count = count_resource_items(visible_items)
            search_status_label.config(text=f"{result_count} 项" if result_count else "无匹配资源")
        ui_call(refresh_visible_items, True)

    def insert_resource_urls(urls: list[str]) -> None: # 将链接追加到 URL 输入框，跳过已存在的行
        existing_lines = {line.strip() for line in url_text.get("1.0", "end").splitlines()}
        new_urls = [url for url in dict.fromkeys(urls) if url and url not in existing_lines] # 保序去重，并跳过已存在的链接
        if not new_urls:
            return

        url_text_content = url_text.get("1.0", "end")[:-1] # 获取 URL 输入框的内容，去掉最后一个换行符
        # URL 输入框为空或最后一个字符为换行符时，插入的内容前面不加换行
        prefix = "" if not url_text_content or url_text_content[-1] == "\n" else "\n"
        url_text.insert("end", prefix + "\n".join(new_urls))
        url_text.see("end") # 滚动到文本框底部

    def remove_resource_urls(urls: list[str]) -> None: # 从 URL 输入框移除已取消勾选资源的链接行
        if not urls:
            return
        url_set = set(urls)
        lines = url_text.get("1.0", "end").splitlines()
        kept_lines = [line for line in lines if line.strip() not in url_set]
        if len(kept_lines) == len(lines):
            return
        url_text.delete("1.0", "end")
        if kept_lines:
            url_text.insert("1.0", "\n".join(kept_lines))

    def update_checked_count() -> None: # 更新已勾选教材数量提示
        checked_count_label.config(text=f"已选 {len(checked_items)} 项" if checked_items else "")

    def toggle_item(item_id: str) -> None: # 切换树项勾选状态：分类按三态决定目标状态并级联其下所有末级资源
        sync_checked_items() # 文本修改事件尚未处理时，也以最新输入为准
        node = tree_item_data.get(item_id) # 搜索时只操作当前筛选出的子树
        if node is None:
            return
        children = node.get("children")
        if children:
            leafs = list(iter_leaf_resources(children, item_id))
            checked = should_check_category([leaf_id for leaf_id, _leaf_data in leafs], checked_items)
        else:
            leafs = [(item_id, node)]
            checked = item_id not in checked_items
        set_items_checked(leafs, checked)

    def set_items_checked(leafs: list[tuple[str, dict]], checked: bool) -> None: # 批量更新末级资源勾选状态，级联刷新图标并同步 URL 输入框
        urls = [leaf_urls[leaf_id] for leaf_id, _leaf_data in leafs]
        if checked:
            insert_resource_urls(urls)
        else:
            remove_resource_urls(urls)
        sync_checked_items()

    def sync_checked_items() -> None: # 粘贴、删除、撤销以及树项操作统一以输入框中的链接为准
        urls = {line.strip() for line in url_text.get("1.0", "end").splitlines()}
        new_checked_items = {item_id for item_id, url in leaf_urls.items() if url in urls}
        if new_checked_items == checked_items:
            return
        with log_duration(logger, "同步勾选状态", CHECK_SYNC_SLOW_MS):
            checked_items.clear()
            checked_items.update(new_checked_items)

            invalidate_item_icons() # 末级资源与各级祖先分类的图标都可能变，屏幕外的等滚进视野再合成
            refresh_visible_items(False)
            update_checked_count()

    def on_urls_modified(_event: tk.Event) -> None:
        if url_text.edit_modified():
            url_text.edit_modified(False)
            sync_checked_items()

    def on_tree_press(event: tk.Event) -> str | None: # 按下鼠标时隐藏悬停提示；左键点击标题或封面（含复选框）时切换勾选，点击箭头或缩进保持展开收起
        hide_tree_tooltip()
        if event.num != 1 or treeview.identify("element", event.x, event.y) not in ("text", "image"):
            return None
        item_id = treeview.identify_row(event.y)
        if not item_id:
            return None
        treeview.focus_set() # 确保随后可以直接用方向键与空格操作
        treeview.selection_set(item_id)
        treeview.focus(item_id)
        toggle_item(item_id)
        return "break"

    def on_tree_space(_event: tk.Event) -> str: # 空格键切换当前焦点树项的勾选状态
        item_id = treeview.focus()
        if item_id:
            toggle_item(item_id)
        return "break"

    tooltip_window: tk.Toplevel | None = None
    tooltip_after_id: str | None = None
    tooltip_preview: ImageTk.PhotoImage | None = None # 持有预览图引用防止被回收
    hovered_tree_item = ""

    def hide_tree_tooltip() -> None:
        nonlocal tooltip_window, tooltip_after_id, tooltip_preview
        if tooltip_after_id:
            runtime.root.after_cancel(tooltip_after_id)
            tooltip_after_id = None
        if tooltip_window:
            tooltip_window.destroy()
            tooltip_window = None
            tooltip_preview = None

    def show_tree_tooltip(item_id: str, x_root: int, y_root: int) -> None: # 悬停时显示完整名称与分类路径
        nonlocal tooltip_window, tooltip_after_id, tooltip_preview
        tooltip_after_id = None
        if item_id != hovered_tree_item or not treeview.exists(item_id):
            return

        path_names = tree_item_paths.get(item_id)
        if not path_names: # 资源目录尚未就绪时的提示行没有分类路径
            return
        tooltip_window = tk.Toplevel(runtime.root)
        tooltip_window.overrideredirect(True)
        tooltip_body = tk.Frame(
            tooltip_window,
            background=theme.current_colors["surface"],
            relief="solid",
            borderwidth=1,
            padx=scaled(10),
            pady=scaled(9),
        )
        tooltip_body.pack()

        preview_cover = preview_cover_pils.get(item_id)
        if preview_cover is None:
            reload_preview_cover(item_id) # 预览图已被淘汰，重新取一次，本次先只显示文字
        else:
            preview_cover_pils.move_to_end(item_id)
            tooltip_preview = ImageTk.PhotoImage(preview_cover)
            tk.Label(
                tooltip_body,
                image=tooltip_preview,
                background=theme.current_colors["surface"],
                borderwidth=0,
            ).grid(row=0, column=0, rowspan=2, padx=(0, scaled(12)))

        text_column = 1 if preview_cover is not None else 0
        tk.Label(
            tooltip_body,
            text=path_names[-1],
            justify="left",
            anchor="w",
            wraplength=scaled(360),
            font="AppStrongFont",
            background=theme.current_colors["surface"],
            foreground=theme.current_colors["fg"],
        ).grid(row=0, column=text_column, sticky="new")
        if len(path_names) > 1:
            tk.Label(
                tooltip_body,
                text=" › ".join(path_names[:-1]),
                justify="left",
                anchor="w",
                wraplength=scaled(360),
                font="AppCaptionFont",
                background=theme.current_colors["surface"],
                foreground=theme.current_colors["muted"],
            ).grid(row=1, column=text_column, sticky="sew", pady=(scaled(8), 0))

        tooltip_window.update_idletasks()
        x = min(x_root + scaled(12), runtime.root.winfo_screenwidth() - tooltip_window.winfo_reqwidth())
        y = min(y_root + scaled(18), runtime.root.winfo_screenheight() - tooltip_window.winfo_reqheight())
        tooltip_window.geometry(f"+{max(x, 0)}+{max(y, 0)}")

    def on_tree_motion(event: tk.Event) -> None:
        nonlocal hovered_tree_item, tooltip_after_id
        item_id = treeview.identify_row(event.y)
        if item_id == hovered_tree_item:
            return
        hide_tree_tooltip()
        hovered_tree_item = item_id
        if item_id:
            tooltip_after_id = runtime.root.after(450, lambda: show_tree_tooltip(item_id, event.x_root, event.y_root))

    def leave_tree() -> None:
        nonlocal hovered_tree_item
        hovered_tree_item = ""
        hide_tree_tooltip()

    search_after_id: str | None = None

    def schedule_search(*_args: str) -> None: # 输入停止片刻后执行筛选，避免连续重建树视图
        nonlocal search_after_id
        if search_after_id:
            runtime.root.after_cancel(search_after_id)

        def run_search() -> None:
            nonlocal search_after_id
            search_after_id = None
            refresh_resource_tree()

        search_after_id = runtime.root.after(150, run_search)

    def focus_search(_event: tk.Event) -> str:
        search_entry.focus_set()
        search_entry.selection_range(0, "end")
        return "break"

    def scroll_tree_horizontally(steps: float) -> str:
        hide_tree_tooltip()
        first, last = treeview.xview()
        treeview.xview_moveto(first + steps * (last - first) * 0.2)
        return "break"

    def on_tree_shift_mousewheel(event: tk.Event) -> str:
        delta_unit = 1 if os_name == "Darwin" else 120
        return scroll_tree_horizontally(-event.delta / delta_unit)

    def apply_resource_list(items: dict[str, dict] | None, status: str = "") -> None: # 填入后台加载到的资源目录并重建树视图；items 为 None 表示目录仍在路上，只更新提示行，status 为当前阶段；items 非 None 时进入终态，status 非空表示加载未成功，改为在树视图中显示原因
        nonlocal resource_list, catalog_status, catalog_loading
        catalog_status = status
        if items is None:
            catalog_loading = True
            update_catalog_status_row()
            schedule_catalog_tick()
            return

        catalog_loading = False # 就绪与失败都是终态：计时到此为止，失败原因不再被秒数改写
        cancel_catalog_tick()
        resource_list = items
        leaf_urls.clear()
        leaf_urls.update({item_id: build_resource_url(item_id, data) for item_id, data in iter_leaf_resources(resource_list)})
        refresh_resource_tree()
        sync_checked_items() # 资源目录就绪前用户可能已粘贴链接，据此恢复勾选状态

    rebuild_checkbox_images() # 构建树项前先生成三态复选框图标
    theme.on_theme_applied(on_theme_changed) # 主题切换后重建复选框配色并刷新树项图标
    update_checked_count()
    refresh_resource_tree() # 初始展示完整资源树并展开一级目录
    schedule_catalog_tick()
    sync_checked_items()
    url_text.edit_modified(False)
    url_text.bind("<<Modified>>", on_urls_modified, add="+")
    search_var.trace_add("write", schedule_search)
    treeview.configure(yscrollcommand=on_tree_view_change)
    treeview.bind("<space>", on_tree_space)
    treeview.bind("<<TreeviewOpen>>", on_tree_open)
    treeview.bind("<Configure>", lambda event: resize_tree_column(event.width))
    treeview.bind("<Motion>", on_tree_motion)
    treeview.bind("<Leave>", lambda _event: leave_tree())
    treeview.bind("<Destroy>", lambda _event: cancel_catalog_tick(), add="+")
    treeview.bind("<ButtonPress>", on_tree_press)
    treeview.bind("<Shift-MouseWheel>", on_tree_shift_mousewheel)
    treeview.bind("<Shift-Button-4>", lambda _event: scroll_tree_horizontally(-1))
    treeview.bind("<Shift-Button-5>", lambda _event: scroll_tree_horizontally(1))
    search_entry.bind("<Escape>", lambda _event: search_var.set(""))
    runtime.root.bind("<Control-f>", focus_search)
    if os_name == "Darwin":
        runtime.root.bind("<Command-f>", focus_search)

    return apply_resource_list
