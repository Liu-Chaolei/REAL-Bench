# Gaia2 当前数据目录与整理记录

状态：数据目录整理已完成；本次按最新决定移除固定异步数据，只保留运行时动态异步交互。

## 保留集合

| 集合 | 数量 | 活动位置 |
| --- | ---: | --- |
| 基础任务 | 800 | `data/tau2/domains/gaia2/tasks.json`，`base` split |
| Mind Change | 100 | 同一主文件，`mind_change` split |
| Repetition | 100 | 同一主文件，`repetition` split |
| 隐藏信息 | 100 | `data/tau2/domains/gaia2_hidden/`，独立运行 |

主文件总量为 **1000**，`all` 为前三项的并集。五个原始类别 split 各 160 题。
不指定 split 时，正式批量运行使用 `base`；低层 `get_tasks()` 可加载完整主文件。

## 活动目录

```text
data/tau2/domains/gaia2/
├── README.md
├── tasks.json                 # 1000 题，唯一主文件
├── split_tasks.json           # base800 / mind_change100 / repetition100 / all1000
├── manifest.json
├── verification.json
├── provenance.json            # 200 道变体的来源
├── variants.validation.json
├── snapshots/                 # 共享初始快照
├── resources*.json
└── assets/                    # 原有资源

data/tau2/domains/gaia2_hidden/
└── ...                        # 独立的隐藏信息100题

data/tau2/metrics/persistent_task/templates/
└── en.json                    # 运行时动态异步的公共语言回退模板
```

模板是动态机制的公共资源，不是一套固定任务，不复制到主数据目录。

## 本次移除

- 主文件中的 100 道固定 async 变体及嵌入请求配置。
- `async` split 和 `all` 中对应的 100 个 ID。
- manifest 中固定 async 的分类计数、旧 qpt ID 映射及专用状态字段。
- 英文任务元数据中对应的 100 个条目。
- `data/tau2/domains/gaia2/persistent_tasks/` 下 10 道固定样例的任务、参考、
  触发、评分、来源和 manifest 六个文件，目录一并移除。

此前独立固定 async 工作流已经迁入主文件，当前不再建立或保留固定异步数据副本。
旧模拟结果和原有归档仍是历史材料，不进入活动数据加载。

## 仅保留运行时动态异步交互

通过 `--active-metrics-config examples/active-metrics.persistent-task.yaml` 启用。
模块观察真实后台任务状态，选择触发意图，生成用户请求，通过普通对话交付，然后使用
实际任务操作和回复证据评分。无需 `async` split 或固定题号。

动态代码和公共模板保留。历史结果的兼容读取代码不等同于活动固定题库。

## 元数据与验证

过滤删除集合后，保留任务的 JSON 内容逐条不变。只更新任务数量、集合成员和文件哈希。
历史参考验证结果只投影到未变的保留集合，并在 verification 中写明继承来源和没有重跑。
不为此次删除任务重新封存资源或运行 800/1000 道参考执行。

轻量确认：主文件1000题、base800题、两个变体各100题、all无删除ID、任务可加载、
动态配置和模板可读、动态异步的相关本地测试通过、数据目录检查通过。
