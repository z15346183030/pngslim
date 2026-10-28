# 灵感来源：[oxipng/oxipng](https://github.com/oxipng/oxipng)

- 星标：4,302
- 许可证：MIT
- 语言：Rust
- 它承诺解决：把 PNG 压到不能再小，像素一个不变。

## 它做对了什么

**1. 把「怎么压」拆成两条互不干扰的轴。**
一条是像素怎么表示（`src/reduction/`：位深、色彩类型、调色板、透明通道、隔行），
一条是同一堆像素怎么写进字节流（`src/filters/` 滤波 + `src/deflate/` 熵编码）。
两条轴可以独立试、独立比大小，最后取总字节数最小的组合。
这个拆分是整个项目能并行、能加 `-o` 等级、能扩展的前提。

**2. 候选不是一个个试，是扔进一个「收集器」里比大小。**
`src/evaluate.rs` 的 `Evaluator` 接收一堆候选图像，每个候选在自己的线程里做
「滤波 → 压缩 → 算最终文件大小」，结果回到主线程按 `(大小, 数据长度, 滤波策略)` 排序取最小。
压缩到一半发现已经比当前最优大了就直接放弃（`best_candidate_size` 是个原子最小值），
这一条让「暴力试」不至于变成「无限慢」。

**3. 滤波策略不只有那五种。**
除了 PNG 标准里的 none/sub/up/avg/paeth，还有 MinSum、Entropy、Bigrams、BigEnt，
以及一个叫 Brute 的：把若干行当成一块，每种滤波都真的压一遍，谁小用谁。
`-o 4` 用的就是 `-f 0,1,6,7,8,9`——标准滤波反而不在列表里。

**4. 只重写需要重写的那部分。**
`aux_chunks` 原样搬运，`--strip` 是可选而不是默认，`-p` 保留时间戳和权限。
一个压缩工具最忌讳「顺手把你别的东西也改了」，它没有这么做。

**5. 一个静态二进制，零运行时依赖。**
Rust 编译出来单文件，扔进 CI、Docker、pre-commit 都能跑。压缩工具如果不能无痛进流水线，
那就只是个玩具。

## 它在哪里让人痛苦

### 痛点一：`-o` 是一条看不见底的旋钮

`--help` 里 38 个参数，其中 `-o` 有 8 档，每一档展开是一串没人看得懂的数字：

```
0   => --zc 5  -f 0,5         --fast
2   => --zc 11 -f 0,1,6,7     --fast
4   => --zc 12 -f 0,1,6,7,8,9 --fast --brute-level 1 --brute-lines 4
6   => --zc 12 -f 0-9                --brute-level 5 --brute-lines 8
```

`zc` 是压缩器等级，`f` 是滤波策略编号，`brute-level` / `brute-lines` 是 Brute 策略的两个参数。
帮助文本对后两个的说法是：

> Set the libdeflate compression level to use with the Brute filter strategy.
> **Sane values are 1-5. Higher values are not necessarily better.**

一个工具自己承认「更大的数不一定更好」，等于承认这个旋钮没法靠推理调，只能试。
于是 issue [#757](https://github.com/oxipng/oxipng/issues/757)（2025 年 12 月开的，14 条回复，至今未关）
第一句话就是：

> How does this compare to optipng?

下面有人认真地解释了架构差异，也有人补了一句「其实 deflate 引擎比参数更重要」。
一个用了十年、四万多人 star 的工具，用户还在问「我该用哪个」，这本身就是答案。

我实测了这条旋钮到底值多少（同一批 72 个文件）：

| 等级 | 输出 | 比上一档省 | 耗时 |
|---|---|---|---|
| `-o 2` | 11,123,331 B | — | 11.3 s |
| `-o 4` | 11,061,030 B | 0.56% | 22.0 s |
| `-o max` | 11,050,122 B | 0.10% | 137.7 s |

从 4 档到 max，时间涨 6.3 倍，多省 0.1%。这个旋钮的后半段基本是在拿电费换小数点后一位。

### 痛点二：它默认会改你的图像头

`-o 4` 在 69 个文件里改掉了 8 个的 IHDR：6 个把 32 位 RGBA 降成 24 位 RGB（因为 alpha 全是 255），
2 个把真彩色换成调色板。这是它的 `src/reduction/` 在做的事，默认开启。

问题不在于「对不对」——它做得对。问题在于这个转换对下游是有影响的：
拿到 PNG 之后按「每像素 4 字节」算内存的代码、按色彩类型分支的着色器、
把 PNG 当纹理上传的引擎，都可能因为这个静默的变化出问题。
issue [#790](https://github.com/oxipng/oxipng/issues/790) 就是在问怎么**阻止**它转成调色板：

> Way to force greyscale instead of indexed?

而在我们这批文件上，全部 reduction 加起来只值 45,412 字节（0.36%）：
`-o 4` 是 11,061,030 B，加上 `--nx` 关掉所有 reduction 是 11,106,442 B。
**为了 0.36%，默认改变了 12% 文件的编码结构。**

### 痛点三：跑完你不知道整体省了多少

issue [#678](https://github.com/oxipng/oxipng/issues/678)（34 条回复，已关闭）：

> the per-file statistics of oxipng makes for quite a cluttered window and rather hard to see
> how much of an overall improvement it had. `-q` stops the flood of file paths, but doesn't
> display anything at all.

它的输出是「一个文件一行」，`-q` 是「一行都不给」。中间那档「给我一个总数」长期不存在，
后来加了 `--json` 才算有个出口——但你得自己 `jq` 一下。
关闭这条 issue 的原因是「要改 API 才能拿到输入输出大小」，也就是说，
一个「汇总」功能被内部数据结构的形状卡了三年。

### 痛点四：中断了就得从头再来

issue [#549](https://github.com/oxipng/oxipng/issues/549)：

> I have run oxipng at my home folder yesterday, but it has take forever and I have abort it.
> Today I started it again, but it starts from the beginning. Because I have absolute no idea
> where it has stop I can not continue without starting from scratch.

维护者的回复很实在：这是脚本该干的事，不该进核心。这个判断未必错，
但它暴露了一件事——**工具没有「这个文件已经压过了」的概念**。
没有这个概念，就没法幂等，也就没法安全地重跑。

### 痛点五：超时旋钮基本没用

`--timeout` 的帮助文本自己写着：

> Note that this does not cut short any operations that are already in progress,
> so it is currently of limited effectiveness for large files with high compression levels.

大文件 + 高压缩等级，正好是最需要超时的场景，而它恰好在那个场景失效。

## 我们的取舍

砍掉的：

| 砍掉 | 为什么 |
|---|---|
| 所有 reduction（位深 / 色彩类型 / 调色板 / 透明通道 / 隔行） | 实测只值 0.36%，却要改 IHDR。不改头，就能给出「像素必然相同」的结构性保证，而不是「我们相信自己的代码」 |
| `-o 0..6` 七档预设 | 换成「便宜的压缩器给所有候选打分，只对冠军用贵的压缩器」，不需要人调 |
| Brute / MinSum / Entropy / Bigrams / BigEnt 这些滤波策略 | 保留了最有效的自适应（逐行在 none/sub/up/avg 里选），实测和逐块暴力差 0.08% |
| `--strip`、`--alpha`、`--out`、`--stdout`、`--preserve`、`--threads` 等 | 默认就不动元数据、不动权限；输出就是原地替换 |
| Zopfli 支持 | 需要额外依赖。这个工具的存在意义就是零依赖 |
| APNG 帧优化 | 帧是独立 chunk，只压第一帧会改动画。明确跳过并说明原因 |

留下的：

| 留下 | 为什么 |
|---|---|
| 五种标准滤波 + 逐行自适应 | 这是收益的大头，也是唯一能纯 Python 做好的部分 |
| 逐块/逐行并行 | 一个 5 MB 截图比二十个图标慢得多，大文件先跑 |
| 输出前自检 | 每次写盘前把结果解回来逐行比对，失败就不写 |
| 汇总输出 | issue #678 想要的东西，默认就给 |

加上的：

| 加上 | 为什么 |
|---|---|
| `--check` | 退出码 1 表示「还有能省的地方」，可以直接当 CI 闸门 |
| `--explain` | 把每个候选滤波的字节数和耗时列出来，回答「这个文件为什么这么大」 |
| 幂等 | 第二次跑同一个目录，输出字节完全不变，报 0 B |
| 单文件、纯标准库 | `curl` 一个文件就能用，不需要任何安装步骤 |

## 核心算法

一个 PNG 的像素数据是「一行一行」存的，每行前面有一个字节说明这行用了哪种预测：

```
原始行:  [10, 12, 15, 18, 22]
滤波 1:  [10,  2,  3,  3,  4]    存的是「和左边像素的差」
滤波 2:  [10,  1,  2,  2,  3]    存的是「和上一行同位置的差」
滤波 4:  ...                     用左、上、左上三个值猜一个，存差值
```

差值的数值小、重复多，deflate 就能压得更狠。但哪种滤波最好完全取决于图像内容：

- 渐变、照片：`avg` 或 `paeth` 赢，因为相邻像素本来就接近
- 二维码、纯色块、文字截图：`none` 赢，因为大片相同字节本来就是最好的输入，
  一差分反而把「一片 0」打散成噪声

实测最极端的一个文件（1200×1200 的二维码，5.76 MB 原始数据）：

| 滤波 | 输出 |
|---|---|
| none | **98,572 B** |
| adaptive | 116,206 B |
| paeth | 121,002 B |
| up | 121,826 B |
| sub | 123,010 B |
| avg | 129,224 B |

选错滤波，代价是 30%。所以「试一遍再挑」不是暴力，是必需。

我们的做法：

1. 六个候选（none / sub / up / avg / paeth / 逐行自适应）各自把整幅图滤波一遍
2. 每个候选的结果用 zlib level 1 压一下，拿字节数当分数
3. 分数最低的那个，用 zlib level 9 正式压，同时试 `Z_DEFAULT` 和 `Z_FILTERED` 两种策略
4. 挑最小的一份写出去，写完解回来逐行比对

第 2 步是关键：用便宜压缩器当裁判。实测在 6 个文件上，
level 1 的排名和 level 9 的排名**完全一致**，而 level 1 只花 level 9 的 1/20 时间。

## 与原版的差异

| 维度 | oxipng `-o 4` | pngslim |
|---|---|---|
| 输出体积（72 文件） | 11,061,030 B（-11.5%） | 11,642,134 B（-6.8%） |
| 耗时（8 核） | 22.0 s | 63.3 s |
| 安装 | 下载二进制或 `cargo install` | 无，一个 .py 文件 |
| 依赖 | libdeflate / Zopfli（C/Rust） | Python 标准库 |
| 是否改 IHDR | 69 个文件里改了 8 个 | 一个都不改 |
| 元数据 | 默认保留，`--strip` 可删 | 永远保留，没有这个开关 |
| 参数个数 | 38 | 9 |
| 调优旋钮 | `-o 0..6/max` 加 5 个底层参数 | 无。`--brute` 只是「全都用贵压缩器」 |
| 默认输出 | 每个文件一行 | 汇总 + 最大收益前五 |
| 二次运行 | 重新压一遍，结果可能微变 | 字节不变，报 0 B |
| CI 闸门 | 无 | `--check`，退出码 1 |
| 候选解释 | `-vvv` 的 trace 日志 | `--explain` 一张表 |
| 改像素的风险 | 结构性消除（不改头） | 同左，且每次写盘前解回来比对 |

体积和速度两项都是原版赢，这是实话：libdeflate 12 比 zlib 9 在**同一份滤波数据**上小 7.3%
（1,235,908 B vs 1,332,570 B），这部分纯 Python 追不回来。

## 许可证说明

oxipng 采用 MIT 许可证。本次只阅读了它的目录结构、`src/lib.rs` 的流程、
`src/evaluate.rs` 的候选收集逻辑，以及 `--help` 暴露出来的参数含义，
**没有复制任何代码**。滤波公式、Adam7 排布、zlib 调用方式均来自 PNG 规范
（ISO/IEC 15948）与 Python 标准库文档。

SWAR 整行加减法（把一行当成一个大整数、每个字节一个 lane）是通用位运算技巧，
实现为独立推导，并有对照朴素逐字节实现的单元测试（`test_swar_subtract_matches_per_byte_loop`、
`test_swar_add_matches_per_byte_loop`）。
