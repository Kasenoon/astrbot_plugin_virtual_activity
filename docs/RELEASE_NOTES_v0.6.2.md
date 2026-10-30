# v0.6.2

社交指数支持小数了。

## 变化

- `social_grow_min` / `social_grow_max` / `social_talk_threshold` / `social_talk_cost`
  全部改成浮点配置，WebUI 里步长 0.1
- 增长算法由「每分钟 `randint(1,2)`」改成「按分钟 `uniform(min,max)`」，
  设成 `0.2` 这种慢速增长也不会被取整吞掉
- 显示保留一位小数：房间视图状态条、`/回家`、`/测试点`、注入给 LLM 的状态块
- 状态值落盘保留一位小数

## 适合谁

觉得默认「每分钟 +1~2」涨太快、一天就攒爆社交值的人。比如改成：

```yaml
social_grow_min: 0.2
social_grow_max: 0.5
social_talk_threshold: 70
```

这样大约 3.5~5 小时才攒到阈值，主动找你说话的频率会低很多。

## 升级

市场/手动更新后无需改配置，旧配置里的整数会按浮点读。
