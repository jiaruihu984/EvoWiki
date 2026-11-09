走查用的静态笔记（不是 EvoWiki 生成的页面）：下面两个代码块的格式和后端 `embed_block` 写出的一样，资源文件在 `assets/` 里。

第一个是一个能点的小交互：

```embedded-html
type: embedded-html
id: z-steps
src: assets/demo-z-steps.html
title: 点一下，看 z 怎么变
height: 400
```

第二个是沙箱探针：它试着读 Obsidian 的页面、调用 require、发外部请求、跳走，并把结果写在自己框里。每一项都应该是「被挡住了」。

```embedded-html
type: embedded-html
id: sandbox-probe
src: assets/sandbox-probe.html
title: 沙箱探针
height: 560
```
