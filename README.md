# YAOF · NanoPi R2S 精简固件

用 **OpenWrt 官方 ImageBuilder** 组装 NanoPi R2S 固件：内核不做修改，只装 Passwall2 + Xray 和必需组件，LuCI 主题用 Outline。

- OpenWrt 版本、目标平台、分区大小和必装包：`profiles/r2s-policy.json`
- 软件包清单：`profiles/packages.r2s`
- 写入固件的文件：`profiles/r2s-overlay/`、`profiles/r2s-status/`

## 发布

在 GitHub Actions 里，选 `main` 分支，手动运行 **R2S-OpenWrt**：

| 选项 | 默认 | 作用 |
| --- | --- | --- |
| `build_firmware` | 开 | 关掉时只解析和审计软件包，不出固件 |
| `publish` | 开 | 校验通过后，把固件上传到以 OpenWrt 版本命名的 Release（同一版本覆盖附件） |
| `upload_source` | 关 | 额外保存官方 ImageBuilder 压缩包（保留 5 天） |

发布不用新建临时分支，也不用改 workflow。同一时间只会有一个发布任务在跑。

## 工作流

| 文件 | 触发方式 | 作用 |
| --- | --- | --- |
| `R2S-OpenWrt.yml` | 手动运行 / 仓库主人点 Star | 构建并发布 |
| `R2S-ImageBuilder.yml` | 被其他工作流调用 | 回归测试 → 组装 → 校验 → 发布 → 只保留最近两个版本 |
| `R2S-ImageBuilder-Check.yml` | 构建相关文件推送到 `main` 时 | 只构建、校验，不发布 |
| `Cleaning.yml` | 每周一 | 清理旧固件、旧 Artifacts 和运行日志 |

## 本地测试

```sh
python3 -B -m unittest discover -s tests -v
```

旧的多机型源码编译流程（R2C/R3S/R4S/X86、内核补丁、SEED 配置）已经移除，需要时可以从 git 历史找回。
