# UnderBOX UI 更新与 boxy

本次界面开发、验证、部署与 Git 提交在服务器完成。采用浅色工作台、柔和紫色点缀、轻边框和分层阴影，替换原来的粗描边、硬阴影和网格布景。保留核验状态的语义配色与规则判定。

## 操作流程

- 欢迎/登录页与上传页使用统一的双栏布局，小屏切换为单栏。
- 核验工作台显示“输入材料 → 确认范围 → 检索证据 → 查看报告”，依据真实界面状态更新，不模拟核验进度。
- 报告逐条展开；企业字段与院校资料使用卡片展示。来源、输入风险与账户设置改为抽屉。
- 来源与导出在手机上仍可访问；左侧概览与证据覆盖默认折叠。
- 恢复报告时明确提示材料状态，避免把尚未恢复的原件预览当成当前文件。
- 新建核验前提供 Popconfirm；配额编辑使用有整数校验的表单弹层；结果与错误使用可关闭的 Toast。
- 图标与工具按钮提供 Tooltip。支持 Esc、Tab 焦点约束和 boxy 的 Cmd/Ctrl+J 快捷键。
- 首次登录后的 Tour 按账户保存在当前浏览器；可跳过，也可从“使用指南”重新开启。
- 核验过程仍保留真实阶段、日志和用量；完成后默认收起，手动保持展开时尊重用户选择。

## boxy 与问答

boxy 是闭合、毛茸茸的小盒子伙伴，使用透明像素风素材与轻微呼吸、摇摆及思考反馈。生成报告前提供操作引导，报告完成后提供具体问题建议。

流式回答使用请求动画帧队列，按真实收到的内容渐进绘制。只有新文字短暂柔化晕染，已读文字保持清晰；不会反复替换整个回答。后端结束时加速排空待显示内容，停止按钮与查询重置会取消绘制。尊重系统“减少动态效果”设置，减少动效时直接显示真实内容。

用户翻看旧消息时停止自动跟随，提供“回到最新回答”入口；回答完成后可复制。中文输入法正在组词时，Enter 不会误提交。模型输出只作为文本写入 DOM，不作为 HTML 执行。

## 文件与验证

- underbox/ui.css：视觉系统、响应式布局、组件与动效。
- underbox/ui.js：Toast、Tooltip、抽屉协调、确认/表单弹层、Tour、导出菜单与 InkStream。
- underbox/index.html：原流程的必要钩子、boxy 入口、问答和流程展示。
- underbox/assets/bocy.png：boxy 透明像素素材。
- underbox/serve.py：补齐图片、SVG 与字体静态资源的 MIME 类型。
- underbox/tests/test_ink_stream.cjs：5 项渲染行为测试，不依赖额外 DOM 库。

运行检查：

    node --test underbox/tests/test_ink_stream.cjs
    node --check underbox/ui.js
    cd open_box
    python -m unittest tests.test_institution_context -v
    python -m unittest discover -s tests

浏览器验证覆盖桌面与 390px 手机宽度：首次 Tour 完成/跳过、材料输入、选择条目、生成报告、boxy 问答/停止、来源抽屉、导出菜单与新建核验确认。使用的是明确标注的合成演示数据；演示服务不部署到生产。

完整 Python 回归中原有的 13 个失败、3 个错误仍与更新前一致；新增机构功能的 30 项测试和水墨流式输出的 5 项测试通过。

## boxy 素材来源

工具：内置 image_gen，透明背景生成模式。最终项目文件在服务器 /opt/underbox-repo/underbox/assets/bocy.png，部署副本在 /opt/underbox/underbox/assets/bocy.png。原图为 RGBA PNG；未使用他人产品的宠物或角色素材。

最终生成提示词：

> Use case: stylized-concept. Asset type: small transparent pixel-art mascot for the collapsed chat launcher of a professional HR web application. Create ONE mascot, front three-quarter view, centered with generous transparent padding. Character name Bocy (do not write any text). A tiny very cute fluffy anthropomorphic CLOSED little box: soft rounded square silhouette, warm oatmeal/cream fuzzy pixel fur, small dark shiny bead eyes, tiny relaxed mouth, subtle rosy cheeks, short stubby arms and feet. One small muted lavender corner patch. The box is fully closed, no lid flaps, no contents visible. Warm gentle reassuring companion, adult-friendly charming pixel game art, meticulously crisp pixel clusters with a limited palette and stepped pixel edges. Hands visible beside body in a small welcoming pose, feet level. No hard cardboard shipping marks, no tape seam over face, no magnifier, no detective outfit, no camera, no keyhole, no surveillance associations, no exposed personal information, no accessories, no background, no floor, no text, no watermark. The body should read as a little plush box friend rather than a scary robot or a shipping package. Production quality pixel sprite, single character only.
