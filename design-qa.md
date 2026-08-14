# 状态区与错误定位 Design QA

- Source visual truth: `2026-08-12_094122.png`（用户提供、保持未跟踪）与本轮确认的 Markdown 双栏/五卡线框。
- Implementation screenshot: `/tmp/trader-status-ui-final/desktop-1440x900.png`。
- Error-detail screenshot: `/tmp/trader-status-ui-final/desktop-error-details-1440x900.png`。
- Viewport: 1440×900 CSS px，deviceScaleFactor 1；另验收 1280×720、1920×1080。
- Pixel dimensions: source 1176×308；implementation 1440×900；两者均按原始 1× 像素查看。源图只覆盖状态卡区域，因此整体页面不做像素级裁剪等同比较。
- State: 深色桌面看板、Long 当前视图、系统降级 2 项、错误详情包含活动与已恢复记录。

## Full-view comparison evidence

源图和实现截图已在同一比较输入中按原始尺寸打开。实现保留源图的深色金融看板、紧凑数值层级、等宽指标卡和黄色降级语义，同时按已确认线框把独立“降级状态”卡合并到顶部“最近错误”双栏。页面其余区域继续使用现有产品设计系统，没有引入新的品牌、字体、图标或装饰资产。

## Focused region comparison evidence

顶部状态区在 1440×900 原始截图中可清晰读取，无需再次放大裁剪；单独检查了固定双栏、五张摘要卡、健康徽标、最高优先级原因及“查看全部”按钮。错误详情另用同视口截图检查三条记录的严重度、策略、阶段、时间、恢复状态、受控代码和复制入口。

## Required fidelity surfaces

- Fonts and typography: 继续使用产品既有中文系统字体栈；标题、指标、辅助文本分别使用 17/13/11/10px 层级，未出现异常换行、裁切或过重字重。
- Spacing and layout rhythm: 双栏均为 82px 固定高度；五卡等宽且保持单行；三档桌面均无页面级横向溢出、重叠或纵向顺序变化。
- Colors and visual tokens: 复用现有 charcoal surface、line、green、amber、red tokens；正常、降级、错误同时以文字、颜色和结构表达。与源图 navy 色相差异属于沿用当前产品设计系统的有意约束。
- Image quality and asset fidelity: 本区域没有新增位图资产；继续使用打包的产品标志和 Lucide 图标资源。源图水印不是产品资产，未复制。
- Copy and content: 主界面只显示中文原因和影响元数据；技术代码只在详情中显示。正常、降级、错误、活动中和已恢复文案可区分。

## Findings

没有剩余 P0、P1 或 P2 设计问题。

## Comparison history

1. 首轮实渲染确认双栏、五卡和错误抽屉布局正确，但 Firefox headless 禁止剪贴板写入，复制按钮只能报告失败，属于 P2 交互缺口。
2. 增加代码文本自动选中降级路径；复验按钮状态为“已选中，请复制”，用户仍可立即使用 Ctrl+C。三档视口、抽屉开关、活动/恢复排序、主界面隐藏原始代码和浏览器错误检查全部通过。

## Follow-up polish

没有阻断性交付项。真实数据源无法提供发生时间时继续显示“时间待确认”，避免伪造时间。

final result: passed
