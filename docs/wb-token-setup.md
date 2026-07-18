# WB API Token 配置

本项目只读取环境变量中的 Token，不在代码、Markdown、JSON、日志或 Git 中保存真实 Token。

## 获取 Token

1. 使用 WB 店铺所有者账号登录 WB Partners。
2. 点击组织名称，打开“Интеграции по API / API 集成”。只有店铺所有者可以创建或删除 Token。
3. 点击“+ Создать токен / 创建 Token”，选择“Для интеграции вручную / 手动集成”。
4. 本地运行的 `wb-store-manager` 选择“Персональный / 个人 Token”；测试沙箱选择“Тестовый / 测试 Token”。不要把个人 Token 交给第三方云服务。
5. 填写可追溯名称，例如 `wb-store-manager-main-2026-07`。
6. 按实际工作勾选最小权限：

   - 商品卡片、类目、特征、标题、描述和图片：`Контент / 内容`。
   - 价格和折扣：`Цены и скидки / 价格与折扣`。
   - FBS 订单、卖家仓库和库存：`Маркетплейс / 市场`。
   - 销售漏斗和搜索分析：`Аналитика / 分析`。
   - 订单、销售和库存统计接口：`Статистика / 统计`。
   - 推广、问答与评价、聊天、供应、退货、文件、财务：仅在对应工作流确实需要时勾选。

7. 纯查询任务选“Только чтение / 只读”；创建或修改卡片、媒体、价格、仓库、库存等任务必须选择“Чтение и запись / 读写”。权限不足时不能修改原 Token，需要重新创建。
8. 创建后立即复制 Token；WB 只显示一次。

官方说明：[创建、更新和删除 Token](https://dev.wildberries.ru/knowledge-base/articles/019d49a1-01c8-7cba-95ca-1b95904886b7/kak-sozdat-obnovit-ili-udalit-token-wb-api)；[Token 类别与权限](https://dev.wildberries.ru/docs/openapi/api-information)。

## 配置位置

单店默认使用用户级环境变量 `WB_API_TOKEN`：

```powershell
setx WB_API_TOKEN "<复制的 WB Token>"
```

执行后关闭并重新打开 PowerShell，再验证：

```powershell
python -m wb_manager ping
```

多店铺必须为每家店使用不同变量名，并在店铺配置中只记录变量名，例如 `WB_TOKEN_WB_MAIN`、`WB_TOKEN_WB_SECONDARY`。禁止把 Token 写进 `.env`、配置样例、截图或提交记录。

## 有效期与轮换

- 普通 WB Token 有效期为 **180 天**；准确到期时间可在 WB 后台或官方 Token 解码器中查看。
- 在到期前 **7–14 天**点击“复制/Дублировать”，用相同权限创建新 Token。
- 先替换环境变量并完成连通性、读权限和必要写权限验证；稳定运行后再删除旧 Token。
- 建议设置到期前 30 天、14 天和 7 天提醒，并每月检查 Token 状态与权限范围。
- 店铺所有者变更、Token 泄露、401 `token expired` 或 Token 被撤销时，立即重新创建并替换。
