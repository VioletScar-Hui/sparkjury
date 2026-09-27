# governance/ — 决策池

这里只放一样东西：`events.jsonl`，看板上每一次拍板落下来的 append-only 事件。

- **谁写**：`POST /runs/{run_id}/decision`（看板三个按钮）和老的 `POST /runs/{run_id}/confirm`。
  落盘位置可以用 `SPARKJURY_LEDGER` 改，默认就是本目录。
- **谁读**：治理层——`sparkjury-prioritize --overrides governance/events.jsonl` 重排优先级，
  `sparkjury-govern proposals` 出 `override_audit`。
- **形状**：`contracts/event-ledger.schema.json`。只追加，按 `event_id`（`evt-0001`…）幂等。
  不要手改已有行：改了就没有「当时那个人确实是这么拍的」这回事了。
- **进不进版本库**：默认不 ignore。它是「人为什么改了排序」的证据，丢一次就没法复盘；团队若不打算留档，
  把 `governance/events.jsonl` 写进 `.gitignore` 即可，代码不用动。
