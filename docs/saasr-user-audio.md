# SAASR 连续多说话人模拟用户

在任意 full-duplex 语音域中，通过开关使用 SAASR 合成用户声音与连续背景。
初始请求、后续回复、澄清和 backchannel 都从当次文本合成；场景 YAML 里的旧主文本、
已有 `target_tts.wav` 和已有多说话人混音不作为当前用户发言播放。

## 准备独立运行环境

τ 不需要安装 PyTorch 或 Qwen3-TTS。使用修改后的 `saasr-pro` 创建独立环境，
安装其普通依赖与 `docker/saasr/requirements-runtime.txt` 中的模型运行时依赖：

```bash
cd /path/to/saasr-pro
python3.12 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/python -m pip install -r docker/saasr/requirements-runtime.txt
```

准备 Qwen3-TTS-12Hz-1.7B-Base 完整权重，以及场景 YAML 引用的对话 JSON、
参考声音和 DEMAND 噪声。运行配置的 `model_dir` 覆盖场景 YAML 中的模型位置，
无需改动原始场景文件。模型只在 worker 首次执行合成时加载一次，后续任务复用模型
和参考声音克隆提示；各任务的背景音轨和播放位置独立。

## 场景映射和开关

复制 `integrations/saasr/runtime.example.json`，填写独立 Python 路径、模型路径和 GPU。
`worker_commands` 是参数数组，不经过 shell。每条命令启动一个常驻进程，`devices`
按顺序指定该进程使用的 GPU。可以增加多条命令分配多张 GPU；同一 GPU 的推理串行执行。

在 τ 中为已有场景建立通用索引：

```bash
UV_FROZEN=1 uv run python -m tau2.voice.saasr_index \
  --root /path/to/saasr-pro/configs/gaia2 \
  --domain gaia2 --output /path/to/scenes.local.json
```

索引格式为 `{"gaia2/<任务ID>": "场景YAML路径"}`。相对路径以索引文件目录为基准。
其他域使用同样格式，例如 `retail/0`。也可手工为多个任务选择同一场景。
若所有任务都使用一个场景，在运行配置中用 `scene_yaml` 替换 `scene_index`；二者只能指定一个。
运行配置中的相对数据路径以配置文件目录为基准。

保留原有运行参数，再增加：

```bash
tau2 run --audio-native --benchmark none --domain gaia2 \
  --audio-native-provider qwen_agent --qwen-agent-config /path/to/qwen.json \
  --user-audio-renderer saasr --saasr-config /path/to/runtime.local.json \
  --task-ids <有对应场景的任务ID> --max-concurrency 1 \
  --save-to saasr-example --audio-taps
```

关闭时使用 `--user-audio-renderer standard` 并去掉 `--saasr-config`；默认就是关闭。
选中任务的全部映射和资源在打开 Agent 之前检查，缺失时报告错误，不自动跳过或替换。
现有 Gaia2 配置有798条，缺少 `0198_np1ttb9tu3n4vsigme0uld5fulbx6bi4` 和
`0888_51l8dcuw8jy68uqr68f725b0mk479ar2`。

Python 调用时，在 `AudioNativeConfig` 设置相同的 `user_audio_renderer` 和
`saasr_config=SaasrConfig.from_file(...)`。使用本地 `max_concurrency` 控制并发通话；
本版不支持 controller 的跨进程 `--workers`，避免重复占用相同 GPU。

## 连续场景行为

背景对话先按原有排程、speaker-level 响度归一化和位置相关 RIR 生成一次。
各组的重叠率和并发人数限制继续单独生效。背景周期至少保留所有背景语音及混响尾音，
不会因原 YAML 的录音结束时间较短而截断句子。环境噪声独立循环，素材较短时不会补成长静音。
循环接缝使用20毫秒交叉淡化；每个 tick 的读取与音频块大小无关。

首条有效主声就绪后开始场景播放，主声保留 YAML 的初始开始偏移。
后续主声不再重复开场偏移，而是在 τ 授权开口时加入当前背景。用户等待、Agent 发言、
工具等待、后续合成和过期候选作废期间，背景继续。用户被打断只停止主声。

主声由同一参考声音生成，每段按 SAASR 单主声规则归一化到 −24 dBFS，再应用固定 RIR。
噪声使用首条有效主声和背景构成的参考窗口校准，之后固定噪声增益；这不是对整次通话
每一时刻都强制保持12 dB。最终混音的共同峰值增益作用于所有分轨。

SAASR 负责主声和连续场景声学条件，τ 不再叠加自己的随机背景、突发声、muffling 或丢帧。
交互间隙的非定向语音和 vocal tic 作为独立插入轨道保留；SAASR PCM 输出直接发送给 Agent，不再经过 Tau 电话格式转换。用户行为、LLM、grounding、工具和奖励定义沿用原有逻辑。

背景台词真实进入 Agent 的输入音频，但不进入正式用户文本，也不标记为主用户正在说话。
正式用户文本只对应主声区间；初始背景前奏不会启动 Gaia2 场景时钟。
当前 Qwen 的异步收听和真实场景计时保留：后续合成及排队等待计入任务时间。
生成过程中有新 Agent 回复时，普通旧候选仍按现有策略作废，不能因为已经合成完而强行播放。

## 结果与故障

每次通话的 `artifacts/task_*/sim_*/audio/saasr/` 保存：

- `scene/`：背景分轨、合并背景、噪声素材和对话排程。
- `utterance_*/`：当次主声、文本、响度与生成信息，包括未播放候选。
- `played_main.wav`、`played_background.wav`、`played_noise.wav`：按实际输出 tick 对齐的分轨。
- `played_mixed.wav`：SAASR 统一增益后的实际混音；`sent_to_agent.wav` 是直接发送给 Agent 的 SAASR PCM 输出。
- `playback.jsonl`：播放位置、块长度、主声标记和共同峰值增益。
- `render.json`：渲染版本、资源、模型和 worker 身份，以及生成与播放信息。

直接构建 orchestrator 且不指定音频输出目录时，证据位于
`data/simulations/saasr-artifacts/<simulation_id>/`。
这些新结果是 `saasr-continuous-v2` 音频条件，不保证与原离线数据逐采样相同。
同一结果目录不允许续跑时改变 renderer、模型、场景、参考声音或 worker 软件。
源随机种子会传到模型推理；相同种子仍不保证跨 CUDA/软件版本逐字节一致。

每个合成请求只提交一次。worker 崩溃或超时记录为基础设施故障，保留已完成轨迹；
不会退回其他 TTS 或播放历史音频。关闭一个通话仅关闭自己的播放与证据；
runner 结束时才回收共享 worker。

## 验证

也可以使用已有 SAASR GPU 镜像而不在宿主机安装模型依赖。将一个 `worker_commands`
条目设为下面的参数数组；`{worker_directory}` 会由 τ 替换为该 worker 的独立目录：

```json
[
  "/path/to/tau2-bench/.venv/bin/python",
  "/path/to/tau2-bench/integrations/saasr/docker_worker.py",
  "--source", "/path/to/saasr-pro",
  "--model", "/path/to/Qwen3-TTS-12Hz-1.7B-Base",
  "--output-root", "/path/to/tau2-bench/data/simulations",
  "--worker-directory", "{worker_directory}",
  "--gpu", "0",
  "--image", "docker.v2.aispeech.com/sjtu/sjtu_chenlu-yunzhe.zhang-saasr:0.0.1"
]
```

此时 `devices` 中使用容器内的 `cuda:0`；宿主机 GPU 由 `--gpu` 指定。
同一个 worker 池使用同一模型、dtype 和 attention implementation。
容器只读取已挂载的代码、资源和模型，输出写入指定目录；无需联网下载模型。
多个 worker 分别填写对应 GPU。关闭时只停止当前 launcher 创建的容器。

先做不调用 Agent 或裁判的真实 TTS 音频检查：

本机使用 `.venv/lib/tau-native` 的音频库；`run_hpc.sh` 已自动设置库路径。
直接执行 Python 检查时，先设置
`LD_LIBRARY_PATH="$PWD/.venv/lib/tau-native${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"`。

```bash
UV_FROZEN=1 uv run python -m tau2.voice.saasr_check \
  --config /path/to/runtime.local.json --domain gaia2 --task-id <任务ID> \
  --text 'Hello, I need your help.' --text 'Yes, please.' \
  --output data/simulations/saasr-audio-check
```

该检查保存连续混音、各段主声及独立背景，不衡量 Agent 准确率。

```bash
# τ：离线合约与语音回归
UV_FROZEN=1 uv run pytest tests/test_voice/test_saasr.py tests/test_checkpoint.py
make test-voice

# SAASR：真实排程、混响、噪声；fake TTS，不需要 GPU
/path/to/saasr-pro/.venv/bin/python -m pytest \
  /path/to/saasr-pro/tests/test_continuous.py
```

真实 GPU 与 Qwen 通话验收需另外提供完整模型和可用的 API 配置。
离线合约通过不等于模型任务完成率通过。改变共享场景语义后，Gaia2 的 runtime seal
需要使用现有 `tau2 gaia2 verify --reference --seal` 流程重新验证，不能直接手改 pin。
