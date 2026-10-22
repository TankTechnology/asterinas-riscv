# 从内核自检到 `aplay`：ALSA PCM 最小面

T3 的后续增量（任务 A）。上一增量让 HDA 自己放音；这一步让**镜像里的
`aplay`**通过标准的 `/dev/snd` 节点出声。

## 实现

`kernel/comps/audio-hda` 新增两个字符设备（Linux `SOUND_MAJOR` 116）：

- `/dev/snd/controlC0`：`PVERSION`、`CARD_INFO`、`PCM_INFO`、
  `PCM_NEXT_DEVICE`、`PCM_PREFER_SUBDEVICE`。
- `/dev/snd/pcmC0D0p`：`PVERSION`、`INFO`、`TSTAMP`/`TTSTAMP`/`USER_PVERSION`、
  `HW_REFINE`、`HW_PARAMS`、`HW_FREE`、`SW_PARAMS`、`PREPARE`、`START`、
  `DROP`、`DRAIN`、`HWSYNC`、`SYNC_PTR`、`STATUS`、`WRITEI_FRAMES`。

参数求解只支持一套格式（48 kHz / S16_LE / 立体声 / `RW_INTERLEAVED`）：
`HW_REFINE` 把 format/access/subformat 掩码收敛到这几个位，并把
sample/frame bits、声道数、采样率固定；`HW_PARAMS` 校验调用方请求的集合包含
这套取值。播放是同步的：`WRITEI` 把数据拷进 DMA 缓冲、启动流、等到
`SD_STS.BCIS` 再返回。

`aplay` 在 `snd_pcm_hw_params()` 里会自动 `snd_pcm_prepare()`，因此
`HW_PARAMS` 成功之后必须马上能接受 `PREPARE`。

## 三个非显然的坑

1. **`SNDRV_PCM_IOCTL_USER_PVERSION`（0x04）**：libasound 打开设备后会写自己的
   协议版本；不实现它，`snd_pcm_open()` 直接以 `ENOTTY` 失败，`aplay` 报
   "audio open error: Inappropriate ioctl for device"。
2. **`HW_REFINE` 必须约束缓冲几何**：`snd_pcm_hw_params_choose()` 对每个区间取
   **最小值**。若不限制 `PERIOD_SIZE`/`PERIOD_TIME`/`BUFFER_SIZE`，选出来的周期是
   0，随后默认软件参数的 `avail_min` 为 0，`snd_pcm_sw_params()` 直接
   `-EINVAL`，表现为 "Unable to install hw params"，且**不会**再发任何 ioctl。
3. **QEMU 的 RIRB 响应计数会卡住命令环**：QEMU 在 `RINTCNT` 条响应后停止
   CORB，只有 `RIRBSTS` 的中断位发生 1→0 才清零计数。之前 `RIRBCTL` 没开中断位，
   中断位永远不置位，于是第 255 条 codec 动词之后命令环永久停摆
   （`corbwp=0 corbrp=0xff`）。修法：`RIRBCTL = RUN | INT_EN`，每条响应后清
   `RIRBSTS`。

## 验收

```sh
make test_x86_audio        # 内核自检：driver 直接播放
make test_x86_audio_alsa   # 无 libasound 的 ioctl 探针
make test_x86_audio_aplay  # Debian 镜像里的 aplay
```

`alsa_probe`（initramfs，直接说 ioctl）的 transcript：

```
ALSAPROBE_CTL_VERSION 0x20009
ALSAPROBE_CARD id=HDA name=Asterinas HDA driver=AsterinasHDA
ALSAPROBE_CTL_PCM id=HDA name=Asterinas HDA subdevices=1
ALSAPROBE_PCM_VERSION 0x20012
ALSAPROBE_INFO name=Asterinas HDA subname=playback
ALSAPROBE_REFINE cmask=0xf07 rate=[48000,48000] channels=[2,2] bits=[16,16]
ALSAPROBE_HW_PARAMS ok
ALSAPROBE_WRITEI frames=24000
ALSAPROBE_PASS
```

`aplay` 门禁（Debian 镜像 + `alsa-utils`，QEMU `-audiodev wav` 抓宿主采样）：

```
$ aplay -D hw:0,0 /root/tone.wav
Playing WAVE '/root/tone.wav' : Signed 16 bit Little Endian, Rate 48000 Hz, Stereo
AUDIO_APLAY=0
capture peak=8000 tone_samples=27542 sign_changes=262
PASS (aplay -D hw:0,0 through /dev/snd/pcmC0D0p, captured tone verified)
```

日常矩阵重跑后 `AUDIO` 行从 `missing` 变为 `ok`（清单会 apt 安装 `alsa-utils`
并用 `aplay -D hw:0,0` 播放一段 440 Hz 方波）。

## 边界

- 同步播放：每个 `WRITEI` 等自己的数据放完才返回，所以宿主抓到的波形块之间
  有短静音；对 `aplay` 的阻塞写语义是合法的，但不是低延迟路径。
- 没有 mmap：`mmap(2)` 不被支持，libasound 走 rw fallback（`mmap_status_fallbacked`）。
- 固定 48 kHz / S16_LE / 立体声；没有录音、没有混音器控件、没有 period 中断
  （完成靠 `BCIS` 轮询）。
- 镜像里的 `aplay` 需要 `alsa-utils`（门禁自己安装）。
