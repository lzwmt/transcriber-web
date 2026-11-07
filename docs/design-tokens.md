# Design Tokens · 转写 App UI（关 3 锁定草案）

状态：**已锁定**（2026-10-30 用户确认。源自 UI 试跑关 3 锁定版，变更记入验收报告）
定版样稿（关 2，2026-10-30 用户定）：Hark 出品两版同骨架、双主题切换
- 浅色「无彩账本」（Taste · achromatic-ledger）
- 深色「录音棚」（Taste · true-black-flat）
切换机制：`<html data-theme="ledger | studio">`，顶栏放切换按钮；**默认跟随系统深浅**（prefers-color-scheme，2026-10-30 用户定），用户手动切换后以 localStorage 的选择为准。

## 共享 tokens（不随主题变）

- 字体栈：`"Geist","Inter",-apple-system,BlinkMacSystemFont,"Segoe UI","Helvetica Neue",Arial,"PingFang SC","Microsoft YaHei","Noto Sans SC",sans-serif`（两版统一取 v2 的完整栈）
- 字号阶梯：12 / 14 / 16 / 20 / 24 / 32（正文 14、逐字稿 16/1.75、其余按阶梯取）
- 容器：max-width 1200、左右 padding 取 --pad（账本 24、录音棚 22）；栅格间距 --gap（见主题）；断点 860px 以下单栏
- 布局比例：输入卡三行——行1「在线录音 / 上传音视频」等宽 1fr/1fr，行2 贴链接整行，行3 档位+开始转写（1fr/auto）；详情区 8fr/4fr；行1 随录音开关变：开关关闭（默认）时行1 单列整行、打开时行1 双列等宽（C 节入口布局改版，协调者放行时 Q5 裁定，2026-11-03）
- 控件高：36（.btn.lg 为 44）；图标自绘 SVG、统一 1.5–1.8 描边
- 动效（Emil 口径）：`cubic-bezier(.23,1,.32,1)`、140–220ms、只动 transform/opacity、按压 scale .97（播放键 .94）；`prefers-reduced-motion` 时全关
- 数据自洽规矩（写进实现要求）：播放进度单一数据源（total/pos 一处定义），波形 140 根按 pct 程序生成，播放头与说话人分轨当前段都由同一 pct 派生，禁止手填比例
- 说话人色：每位说话人一个低饱和 tint/ink（头像底 + 分轨色），当前说话段改用主题 --data 色

## 主题 A · 账本（浅色）

```css
--ground:#f2f4f7; --surface:#ffffff; --raised:#eceef2;
--border:#e3e6eb; --rule:#eff1f4; --control:#d4d8de;
--text:#0a0a0a; --muted:#646a73; --faint:#8b919a;
--accent:#141414;            /* 主按钮：墨黑底白字 */
--data:#1d5fbf; --data-pale:#eef3fb;   /* 唯一数据色：只给「正在播放」 */
--pos-bg:#dcfae5; --pos-ink:#04250e;
--neg-bg:#ffedea; --neg-ink:#831c1e;
--warn-bg:#fffbeb; --warn-ink:#92400e;
--neu-bg:#eceef2; --neu-ink:#3d3d3d;
--r-card:8px; --r-ctl:6px; --gap:12px; --pad:24px;
波形高 56px；列表行高 52px；h1 24px
```

## 主题 B · 录音棚（深色）

```css
--ground:#0e0d0c; /* body 底 #070707 */ --surface:#181716; --raised:#2a2826;
--border:#211f1d; --rule:#211f1d; --control:#44413d;
--text:#ffffff; --muted:#b4b4b4; --faint:#949494;
--accent:#ffffff;            /* 主按钮：反白（白底黑字） */
--data:#f2b33d; --data-pale:#2a2414;   /* 录音灯琥珀：只给播放与当前段 */
--pos-bg:#1d2a17; --pos-ink:#9fd67a;
--neg-bg:#2c1a1c; --neg-ink:#f0717a;
--warn-bg:#2b2414; --warn-ink:#e5a93b;
--neu-bg:#2a2826; --neu-ink:#ffffff;
--r-card:20px; /* 外壳 frame 16px */ --r-ctl:999px（胶囊）; --gap:14px; --pad:22px;
波形高 72px；列表行高 64px；h1 20px；播放键琥珀底 + 6px 光环 #2a2414
```

## 与样稿的两处口径修正（关 4 实现时执行，不改样稿文件）

1. 上传区文案：样稿写「单个最长 4 小时」，按产品口径改回「单条最长 1 小时 · 单文件 ≤ 2GB」。
2. 字体栈以本文件共享栈为准（C 版样稿栈少 Geist 与 Segoe UI，实现时统一）。
