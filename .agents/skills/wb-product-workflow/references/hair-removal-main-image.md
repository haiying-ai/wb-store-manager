# 脱毛仪主图规范

凡 WB 脱毛仪/IPL 品类主图的规划、生成、修改、评分、上传和复核，必须执行本文件。不得把其他品类的简洁白底主图规则覆盖本规范。

## 强制流程

1. 锁定具体 SKU、真实颜色、真实机身结构和真实配件；不同颜色 SKU 分别处理。
2. 使用对应 SKU 的真实产品图作为产品外观权威参考。禁止把其他颜色机身直接染色冒充。
3. 核对所有俄文、参数、单位和包装清单，只使用已确认事实。
4. 先生成主图，再逐项复核产品、文字、参数、配件、缩略图效果和竞品表现。
5. 若用户要求预览确认，确认前不得上传。否则只有在用户明确授权线上修改后才能上传。
6. 只替换对应 SKU 的第 1 张图；不得把白色图传到银色 SKU，反之亦然。
7. 上传后回读图片数量和顺序，并从 WB 图片地址重新下载第 1 张目视核验；接口成功不等于完成。
8. 保存生成源图、上传请求/响应、回读结果和线上下载图到 SKU 的 `03上线资料`。

## 产品真实性

- 严格保持真实机身比例、轮廓、按键、指示灯、控制面板、装饰环、出光窗口和散热孔。
- 禁止移植其他型号的显示屏、手柄、冷敷头、出光口或功能结构。
- 白色保持中性白，不受背景染色。
- 银色必须使用真实银色产品参考，呈中性、明亮、细腻的磨砂金属质感；不得偏蓝、发乌、灰蒙或过度镜面化。
- 紫色等其他颜色必须匹配真实 SKU，不得自行偏色。
- 配件只展示真实包装内容，不得增删。

## 版式与信息层级

- 默认画布 `1080 × 1440`，3:4 竖版。
- 主图必须同时承担商品识别和购买理由表达，不能只有产品和装饰背景。
- 推荐层级：品类/型号 → 产品主体 → 第一购买理由 → 关键参数 → 包装清单 → 颜色标识。
- 产品位于视觉主区并占足够面积；参数使用整齐、留白充足的信息卡片；配件清单置于独立底部区域。
- 手机端缩略图中仍须识别产品、型号和核心数字。
- 同型号不同 SKU 使用统一模板，只改变真实产品、颜色名称及已确认差异。

## 色彩方向

- 默认采用温柔、明亮、高级的女性美容仪器氛围：暖粉、柔和玫瑰粉、浅蜜桃、淡薰衣草渐变，配合柔光和简洁弧形光带。
- 标题后方允许稍深的灰粉/淡紫灰区域，以保证白字对比度。
- 背景要鲜亮、干净、有层次，禁止刺眼电蓝霓虹、脏灰、暗黑、灰蒙和低对比度。
- 可参考竞品的情绪和色彩，但禁止复制竞品人物、产品、版式、文案和宣传点。

## 文字与事实

- 俄文必须逐字清晰，无乱码、缺字、重复和变形。
- 不得改写数字、标点、小数逗号、破折号或平方符号。
- 颜色名称必须对应 SKU，例如：白色 `Белый`，银色 `Серебристый`，紫色 `Фиолетовый`。
- 禁止虚构医疗效果、认证、冷敷、嫩肤、祛痘或永久脱毛等未经确认内容。

AI22 当前已确认文案模板：

```text
Фотоэпилятор
IPL AI22
[Белый / Серебристый]
3 уровня • 3 режима
3 уровня
3 режима
Окно 5,2 см²
Лёгкий корпус — 0,19 кг
От сети
В комплекте:
адаптер • очки • инструкция
```

产品参数或配件变化时，以最新已确认 SKU 事实替换，不得机械套用 AI22 数据。

## 完整通用提示词

将方括号替换为已确认 SKU 事实，并明确每张参考图的角色：

```text
Use case: precise-object-edit
Asset type: Wildberries IPL hair-removal-device main image
Canvas: 1080 × 1440 px, 3:4 portrait

Input images:
- IMAGE 1: exact approved layout, background, cards and composition target.
- IMAGE 2: authoritative reference for the current SKU's true device geometry, color and material.
- IMAGE 3 if supplied: authoritative accessory reference only.

Primary request:
Create the Wildberries main image for [MODEL] [COLOR SKU]. Preserve IMAGE 1's information density, layout and gentle premium beauty-device style. Use IMAGE 2 as the only authority for product appearance. If IMAGE 1 shows another color SKU, change only the true device, color label and confirmed SKU differences.

Verified product facts:
- Category: Фотоэпилятор
- Model: [MODEL]
- Color: [RUSSIAN COLOR]
- Levels: [LEVELS]
- Modes: [MODES]
- Window: [WINDOW]
- Weight: [WEIGHT]
- Power: [POWER]
- Package contents: [CONTENTS]

Product truth constraints:
Preserve the exact real proportions, silhouette, controls, indicator lights, decorative bands, IPL window, vents and all structural details from IMAGE 2. Never redesign the device, transplant parts from another model, or recolor another SKU as a substitute. Keep white neutral white; render silver as clear neutral satin silver with bright clean highlights; match all other colors exactly to their real SKU references.

Composition:
Place category and model at top left, make the product the dominant visual subject on the right, place aligned parameter cards on the left, and use a separate package-content strip at the bottom. Keep generous spacing and strong mobile-thumbnail readability. A simple round pedestal is allowed.

Background and mood:
Use a gentle, bright, premium feminine palette of warm powder pink, muted rose, soft peach and pale lavender gradients with diffused studio light and elegant curved light bands. Maintain contrast behind white text and white cards. Avoid electric-blue neon, dark black, muddy gray, haze, low contrast, or excessive saturation. Do not copy a competitor's layout, model, product, copy or claims.

Text (verbatim):
“Фотоэпилятор”
“[MODEL]”
“[RUSSIAN COLOR]”
“[LEVELS] • [MODES]”
“[LEVELS]”
“[MODES]”
“Окно [WINDOW]”
“Лёгкий корпус — [WEIGHT]”
“[POWER]”
“В комплекте:”
“[CONTENTS]”

Text constraints:
Render every Russian character, number, decimal comma, punctuation mark, dash, unit and superscript exactly. Do not translate, paraphrase, abbreviate, add promotional copy or generate malformed text.

Accessory constraints:
Show only the verified accessories, using their true appearance and natural scale. Do not add or omit any item.

Avoid:
wrong model; wrong SKU color; altered geometry; invented functions or certification; medical, cooling, rejuvenation, acne or permanent-removal claims without verified evidence; people; before/after skin; logo; watermark; platform mark; extra badges; unreadable Russian; duplicated or floating objects.

Output:
One finished, high-resolution 1080 × 1440 PNG suitable for Wildberries photo position 1.
```

## 同型号颜色变体提示词

已有确认母版时，优先使用精确编辑：

```text
IMAGE 1 is the exact approved layout target. IMAGE 2 is the authoritative reference for the true [COLOR] device appearance only.

Change only:
1. replace the product body with the correct [COLOR] SKU appearance from IMAGE 2 while preserving IMAGE 1's position, scale, geometry and perspective;
2. replace the color label with exactly “[RUSSIAN COLOR]”.

Keep the 1080 × 1440 canvas, background, cards, icons, pedestal, accessories, layout and all other verified Russian text unchanged. Do not use IMAGE 2's page design. Do not redesign the product or add/remove any claim, accessory, icon, badge or feature.
```

## 生成后评分与硬性门槛

按 100 分复核：

- 产品/SKU真实性：30
- 俄文和参数准确性：20
- 产品突出度与缩略图识别：15
- 信息层级和可读性：15
- 色彩、光影和高级感：10
- 配件真实性与完整度：5
- 与竞品相比的点击吸引力：5

上传门槛：总分至少 `93/100`，且必须高于当前竞品主图得分。

以下任一项直接判定不合格，不考虑总分：错误型号、错误颜色、错误产品结构、错字/乱码、错误参数、虚构卖点、错误或缺失配件、关键信息在缩略图不可读、背景灰蒙或产品被严重染色。未达标必须重做并重新评分，禁止上传。

## 上传后验证

- 确认目标 `vendorCode/nmID` 属于正确颜色 SKU。
- 仅替换 photo #1，保留其余图片和顺序。
- 保存上传请求、响应和首次 API 回读。
- 等待传播后再次回读照片数量与第 1 张 URL。
- 给 URL 加时间戳参数下载线上第 1 张并目视比对批准源图。
- 只有源图、API 状态、照片数量/顺序和线上目视图一致时，才报告完成。
