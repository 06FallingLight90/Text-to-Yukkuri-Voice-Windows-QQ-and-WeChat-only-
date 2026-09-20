# 移动端语音注入调查：Hook 面静态验证

日期：2026-09-20 · 设备：小米 14（23127PN0CC，Android 14，arm64-v8a，未 root）
目标应用：微信 8.0.77（com.tencent.mm）/ QQ 9.3.55（com.tencent.mobileqq）

## 背景与目标

移动端移植采用方案 C（LSPosed 模块注入 PCM），参考 [MicInject](https://github.com/xixiya22/MicInject)（hook Java 层 `AudioRecord.read()`）。核心疑问：**微信/QQ 的语音消息录音到底走 Java `AudioRecord` 还是 native OpenSL ES / AAudio？** 这决定 MicInject 的 Java hook 是否直接可用。

## 方法（无需 root 的静态分析）

1. `adb pull` 拉取两个 base.apk（本目录不含 APK 本体，见文末红线说明）
2. `scan_apk.py`：扫描 `lib/arm64-v8a/*.so` 的导入表与符号（libOpenSLES.so、slCreateEngine、libaaudio.so 等）+ dex 中 `AudioRecord`/`MediaRecorder` 引用计数
3. `androguard` 4.1.4 交叉引用分析（`xref_apk.py`）：找出**直接调用**录音 API 的具体类与方法
4. `dump_calls.py`：对身份不明的混淆类 dump 出边调用，人工判定用途

## 结论

### QQ 9.3.55 —— Java hook 可行（高置信度）

语音消息（PTT）路径实锤走 Java `AudioRecord`：

```
com.tencent.mobileqq.ptt.QQRecorder$RecordThread → AudioRecord.read()
```

类名即语义（ptt = push-to-talk）。其余 Java 调用者是短视频/AR/表情录制等旁路功能；native OpenSL ES 库（libGVoice.so、libjingle）服务于通话，与语音消息无关。**fork MicInject 对 QQ 基本开箱即用。**

### 微信 8.0.77 —— Java hook 大概率无效

三层证据：

1. **Native 侧**：`libwxaudio.so`（语音消息引擎）、`libmmmedia.so`、`libmmmediasdk.so`、`libvoipMain.so` 等均直接链接 `libOpenSLES.so` 并调用 `slCreateEngine`——录音发生在 native 层
2. **Java 侧 `AudioRecord` 调用者全部能对号入座**（见 `results/wechat-audio-xref.txt` + `results/wechat-recorder-calls.txt`）：
   - VoIP 通话：`plugin/voip`、`org/webrtc`、`liteav`
   - 视频拍摄：`Lo51/l1`（MediaRecorder）、短视频 AudioCapture
   - 混淆工具类：`Lyl/w`（AEC/AGC/NS 音效链）、`Lcm/e`、`Lsz/g`（音频焦点管理）——疑似听写/语音输入类，未定论
3. 语音消息的 SILK 编码在 native 完成，PCM 无需回 Java 层

### 对方案 C 的影响

| 目标 | hook 层 | 技术 |
| --- | --- | --- |
| QQ 语音消息 | Java `AudioRecord.read` | LSPlant（LSPosed 标准 Java hook） |
| 微信语音消息 | native OpenSL ES 录制回调 | ShadowHook / Dobby（模块附带 native 库） |

微信侧残余不确定性（混淆类身份）需 root 后 frida 动态实测定案；静态侧已到收益递减点。

## 建议路线

1. root（小米 BL 解锁有等待期，尽早启动）→ 装 LSPosed
2. QQ 全链路 MVP：fork MicInject，静态 WAV → 语音气泡先跑通
3. 接动态合成（Termux Node 或 WebView + v86；注意 WebView 内存上限与辞书资产缓存）
4. 微信 native hook 二期：frida 验证后加 ShadowHook 层
5. 合规：APK/仓库不捆绑 AQUEST 引擎与音库，引导用户自行获取

## 产物清单

| 文件 | 说明 |
| --- | --- |
| `scan_apk.py` | so 导入表/符号 + dex 引用计数扫描 |
| `xref_apk.py` | dex 交叉引用：录音 API 的直接调用者 |
| `dump_calls.py` | 指定类的出边调用 dump（判定混淆类用途） |
| `results/wechat-audio-xref.txt` | 微信：AudioRecord/MediaRecorder 调用者清单 |
| `results/qq-audio-xref.txt` | QQ：同上（含 ptt.QQRecorder 实锤） |
| `results/wechat-recorder-calls.txt` | 微信：混淆录音类的出边调用明细 |

## 复现

```powershell
adb pull "<pm path com.tencent.mm 输出的 base.apk 路径>" wechat-base.apk
adb pull "<pm path com.tencent.mobileqq 输出的 base.apk 路径>" qq-base.apk
pip install androguard==4.1.4
python scan_apk.py wechat-base.apk qq-base.apk
python xref_apk.py wechat-base.apk   # 输出含 loguru 调试噪音，按 "dex]" / "<-" 过滤
python dump_calls.py wechat-base.apk
```

## 红线说明

微信/QQ APK 为专有二进制（合计约 670MB），**不入库、不外发**；本目录仅收录自产分析脚本与提取的调用关系文本。AQUEST 引擎/音库照旧不入库（见 THIRD_PARTY_NOTICES.md）。
