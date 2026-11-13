# 参与贡献

Wildberries 店铺运营工具包：财务核算、广告、日报、选品与定价。

## 环境

- Python `>=3.12`
- 依赖为空，`pip install -e .` 即可获得 `wbm` 与 `wb-intel` 两个命令
- 真实店铺凭证放在 `config/` 下的本地文件，仓库里只有 `*.example.json`

```bash
pip install -e .
python -m pytest
```

## 仓库里的数字都是示例

`config/finance/*.example.json`、`config/pricing/*.example.json` 与文档里的费率（佣金、代运营、提现、运费、目标利润）全部是占位值。**提交时不要把自己店铺的真实费率、SKU 数与成本表写回来** —— 这是本仓库历史上唯一一次需要重写 git 历史的原因。

## 改动时的约定

- 金额计算集中在 `wb_manager` 的财务模块，不要在脚本里散落 `* 0.15` 这类魔法数字
- 新增配置项必须同时给 `.example.json` 与 README 说明
- 涉及 Excel 输出请保持列顺序与现有模板一致（下游有人按列读）

## 提 PR

- 说明改动对应的业务场景与口径（税前/税后、含不含退款）
- 附 `pytest` 输出
- 不要提交 `data/` 下的真实报表

安全问题请走 **Security** 页私密报告。
