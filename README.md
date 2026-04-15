# ios_deals

面向新版青龙的 iOS 工具类优惠监控项目。

项目包含两条独立任务链：

- **iOS 工具线索摘要**：抓取 Reddit / RSS / Apple 榜单，AI 预筛后做 Apple 多区价格核验，推送值得关注的工具类优惠线索
- **iOS Watchlist 定向盯价**：按自定义列表持续监控目标 App 的价格变化，支持限免 / 降价 / 达到目标价提醒

---

## 功能

- iOS 工具类优惠线索抓取
- Apple 多区价格核验
- AI 预筛
- 真实限免 / 真实降价判断
- Watchlist 定向盯价
- 青龙 `notify.py` 通知

---

## 青龙要求

仅支持使用 `/ql/data` 目录结构的新版青龙。

本项目按以下路径设计：

```text
/ql/data/scripts/ios_deals
/ql/data/db/ios_deals.db
/ql/data/scripts/notify.py
