# GAIA2 新场景人工预览：改主意 2 题、重复表达 2 题

本次为四题草稿，供人工查看。每题仍是原生的三阶段 Task，保留与原题相同的字段、参考事件、证据、检查和评分标准。只改写一段用户请求及其对应 user 事件，设置独立任务 ID 和新的场景类别，并更新任务描述。原有 800 题和运行、评分流程不变。

计划新增 `mind_change`、`repetition` 两个场景类别，各 100 题，与原来的五类场景并列，总计 1000 题。改主意的变化全部写在同一段用户请求中；重复表达沿用普通用户和阶段流程。这四题计入后续 200 题，尚未合并至正式数据。

本次改写均使最终业务目标与原题一致，便于直接对照语义理解。英文为实际任务内容，中文为人工阅读释义。

配套文件：`tasks.json` 为四个完整原生任务，`provenance.json` 仅保留原题 ID、原题哈希、变体类型、子类型和修改说明；哈希使用原题完整 JSON 的 UTF-8、键排序、无额外空白的 SHA-256。

## 改主意 1：同一段请求中修改时间

- 新题 ID：`0053_aanknwl7oew08yda60lfw6ii37zmbn27__mind_change_v1`
- 原题 ID：`0053_aanknwl7oew08yda60lfw6ii37zmbn27`；原场景：`execution`
- 新场景：`mind_change`；子类型：`parameter_change`
- 改写位置：第 3 段用户请求，其余两段保持原文。

### 改写前的该段请求

Create a 30-minute calendar event on 21 October 2024 at 15:00 UTC titled "2019 Travel Catch-up with Beatriz Silva", with Beatriz as an attendee. Send Beatriz a Chats message proposing that catch-up and asking where we should travel next. This is a proposed appointment; do not claim she has accepted.

### 新题完整三段请求

**第 1 段（原文）**

In my "Travels" folder, each photo is named after the destination and the year of my visit. Please find the youngest contact in my list from each country I visited in 2019, and send them a message on the platform where I had any conversations with them containing the relevant photos from their country along with the message: "Long time no see! Remember this?" If there are no contacts from a particular country, no message is required. In the event of a tie for the youngest contact from a given country, please send a separate message to each person in the tie. If I have conversations in both platforms, prefer Messages. Once all messages have been sent, create a new folder within the "Travels" folder titled "2019" and move all photos from that year into the new folder.

**第 2 段（原文）**

Email me a 2019 Travel Index with one clearly labeled block for Canada, Czech Republic, Jordan, Kenya, Morocco and Portugal. Include the actual photo filename, youngest contact or tied contacts from each country, and the app actually used for the photo message; say "no contact found" where applicable. Include the current 2019 photo folder path.

**第 3 段（改写）**

Create a 30-minute calendar event on 21 October 2024 at 14:00 UTC titled "2019 Travel Catch-up with Beatriz Silva", with Beatriz as an attendee. Actually, make that 15:00 UTC instead; keep the date, title, attendee and 30-minute duration the same. Send Beatriz a Chats message proposing the catch-up at that time and asking where we should travel next. This is a proposed appointment; do not claim she has accepted.

### 改写段中文释义

创建一个 2024 年 10 月 21 日 14:00 UTC 开始、时长半小时、与 Beatriz Silva 的旅行叙旧日程。等等，改成 15:00 UTC，日期、标题、参会人和时长都不变。再通过 Chats 向她提议这个时间，并问她接下来想去哪里旅行；不要声称她已经接受。

### 人工查看重点

创建一个 15:00–15:30 UTC 的日程，并向 Beatriz 发送与这个最终时间一致的 Chats 消息；不创建 14:00 的日程，也不声称她已经接受。

## 改主意 2：同一段请求中替换房源

- 新题 ID：`0052_snsq4rd73gytoo0wr75bgd2nyzra07z1__mind_change_v1`
- 原题 ID：`0052_snsq4rd73gytoo0wr75bgd2nyzra07z1`；原场景：`execution`
- 新场景：`mind_change`；子类型：`selection_replacement`
- 改写位置：第 3 段用户请求，其余两段保持原文。

### 改写前的该段请求

Mechelen Modern in zip 2800 is listed at $1,800 with laundry, which is cheaper than Mechelen Maison. Please add Mechelen Modern back to my saved list so it sits alongside Mechelen Maison, then tell me how many properties are in my saved list now and confirm that both Mechelen properties are on it.

### 新题完整三段请求

**第 1 段（原文）**

Find zip codes with a violent crime rate from 5 to 10. Find all properties with those zip codes and save them if they are not saved yet. If any of those properties were already saved previously, just delete them from the saved list and do not save again. Then, among the ones you newly saved, find the two cheapest ones and send their prices and names to my relatives - they all have the same last name as mine and we live in the same city. Send each of them an individual email letting them know that these are the properties with the low violent crime rates. Title the email message "Properties List". If there is a tie in property price, choose the ones that have laundry, order them by name alphabetically and select the first two. If there are less than two with laundry, pick the one with the most amenities overall.

**第 2 段（原文）**

Please compare the two properties you just emailed me about—Mechelen Mansion and Mechelen Maison—and tell me which one has more amenities. Then remove Mechelen Mansion from my saved list, keep Mechelen Maison saved, and confirm the change.

**第 3 段（改写）**

Please add Mechelen Mansion back to my saved list alongside Mechelen Maison. Actually, I have changed my mind: add Mechelen Modern instead of Mechelen Mansion. The Modern listing in zip 2800 is $1,800 with laundry, which is cheaper than Mechelen Maison. Keep Mechelen Maison saved, then tell me how many properties are in my saved list and confirm that Mechelen Modern and Mechelen Maison are both on it.

### 改写段中文释义

把 Mechelen Mansion 加回收藏，和 Mechelen Maison 放在一起。等等，我改主意了，改加 Mechelen Modern，不加 Mansion。Modern 在 2800 邮区，租金 1,800 美元且有洗衣设施。保留 Maison，然后告诉我收藏总数，并确认 Modern 和 Maison 都在。

### 人工查看重点

新增收藏 Mechelen Modern，保留 Mechelen Maison，Mechelen Mansion 保持未收藏；最终收藏总数仍为原题的 33。

## 重复表达 1：同一段请求中反复要求发邮件

- 新题 ID：`0007_tb52xggog13zgslxysl2jpf7k9h3wgz0__repetition_v1`
- 原题 ID：`0007_tb52xggog13zgslxysl2jpf7k9h3wgz0`；原场景：`search`
- 新场景：`repetition`；子类型：`within_request_repetition`
- 改写位置：第 3 段用户请求，其余两段保持原文。

### 改写前的该段请求

Please send an email to Kaida Rao confirming our lunch on Friday 29 November 2024 at Lysverket from 12:30 to 14:00, and mention that it's at my most-visited lunch spot this year.

### 新题完整三段请求

**第 1 段（原文）**

What location do I spend the most time this year up till now for lunch? If there is a tie, sort the locations alphabetically and give me the top answer.

**第 2 段（原文）**

Takk! One more calendar thing: please book a lunch with Kaida Rao on Friday 29 November 2024 from 12:30 to 14:00, titled "Lunch with Kaida Rao", tagged Social, at the same place you just told me is my most-visited lunch spot this year, with her as attendee — and confirm back to me where it's booked.

**第 3 段（改写）**

Please send an email to Kaida Rao confirming our lunch on Friday 29 November 2024 at Lysverket from 12:30 to 14:00, and mention that it is my most-visited lunch spot this year. Send Kaida that lunch confirmation email, please. I want that confirmation email sent to her with those details.

### 改写段中文释义

给 Kaida Rao 发一封邮件，确认 2024 年 11 月 29 日周五 12:30–14:00 在 Lysverket 吃午餐，并提到这里是我今年最常去的午餐地点。请把那封午餐确认邮件发给 Kaida。我希望把包含这些细节的那封确认邮件发给她。

### 人工查看重点

给 Kaida 发一封包含指定午餐信息的确认邮件；三次表达指向同一封邮件，不产生三封邮件。

## 重复表达 2：换说法重申建群和发消息

- 新题 ID：`0071_732zp6w2fvqcgejn68zrdoolojrbghrh__repetition_v1`
- 原题 ID：`0071_732zp6w2fvqcgejn68zrdoolojrbghrh`；原场景：`execution`
- 新场景：`repetition`；子类型：`paraphrased_repetition`
- 改写位置：第 2 段用户请求，其余两段保持原文。

### 改写前的该段请求

Please create a group conversation called "Project Update Meeting – Nov 1" with Rohan Sengupta and Kipenzi Mwanga, and post the rescheduled details there: Friday, Nov 1, 2–4 pm on Zoom, along with a note that an English PC copy of Snake Oil Game has been ordered for each of them.

### 新题完整三段请求

**第 1 段（原文）**

Reschedule the last "Project Update Meeting" event this month to Nov 01, 2pm-4pm, with other details remaining unchanged. Then, send an email to inform the attendees of this event about the change and ask for their confirmation of attendance. Buy one English version of the PC platform "Snake Oil Game" for me and one for each of the attendees of the "Project Update Meeting" event and apply the discount code if one is available. Let me know after you finish the purchase. 

**第 2 段（改写）**

Please create a group conversation called "Project Update Meeting – Nov 1" with Rohan Sengupta and Kipenzi Mwanga, and post the rescheduled details there: Friday, Nov 1, 2–4 pm on Zoom, along with a note that an English PC copy of Snake Oil Game has been ordered for each of them. Put Rohan and Kipenzi together in that group and share those meeting details and the game-order note with them there. That is the group I need for the update, with that announcement posted in it.

**第 3 段（原文）**

Also add a 30-minute prep sync to my calendar for Thursday, Oct 31 at 4:00–4:30 pm titled "Project Update Meeting – Prep Sync", tagged Work, located at Zoom Meeting, with Rohan Sengupta and Kipenzi Mwanga as attendees; the description should say that we will review the agenda for the Nov 1 meeting and confirm that everyone has their copy of Snake Oil Game.

### 改写段中文释义

建一个名为“Project Update Meeting – Nov 1”的群，把 Rohan 和 Kipenzi 加进去，发消息说明会议改到 11 月 1 日周五 14:00–16:00、在 Zoom 举行，并告知已为他们各买了一份英文 PC 版 Snake Oil Game。把他们放进这个群，在群里分享这些会议信息和游戏订购说明。我需要的就是这个更新群，以及发在里面的这条通知。

### 人工查看重点

创建一个指定名称和成员的 Messages 群，并在其中发布一条包含指定信息的公告；不重复建群或重复发公告。
