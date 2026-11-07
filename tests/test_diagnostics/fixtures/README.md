# 意图评测回归样本

本目录仅供单元测试使用，不调用真实模型或读取本机密钥。

| 样本 | 用途 |
|---|---|
| custom.schema.json | 验证任意自定义结构 |
| custom_intent.profile.json | 验证自定义结构的原子计数和局部引用 |
| email/ | 手写邮件 GT/预测，覆盖改名、顺序变化、错误值、遗漏与新增 |

正式共享定义位于 src/harbor/intent/definitions/gaia2/。
样本只验证当前抽取协议及双向原文核对。
