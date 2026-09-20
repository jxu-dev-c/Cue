# 做了个自动找字幕、对时间轴、生成双语字幕的小工具：Cue

用 Real-Debrid 配 Stremio，或者用网盘比如Pikpak搭配播放器看电影、追剧，字幕经常是最折腾的一步。

好不容易找到字幕，放进去才发现对白和字幕各说各的；我自己又比较喜欢中英双语，但网上能找到的双语字幕确实不多。不想把每看一集都重新来一遍找字幕、调时间轴、翻译、改文件名这套流程，就做了这个。

Cue在本地运行，通过网页操作。接入 WebDAV 或本地视频目录，选好影片和目标语言，就可以批量处理：

- 优先使用视频旁边已有的外挂字幕，没有的话去 OpenSubtitles 找。
- 优先匹配对应视频版本，需要时自动校准时间轴。
- 把英文字幕翻译成中文等目标语言，可以只保留译文，也可以生成双语字幕。
- 自动保存带语言标记的字幕文件，已有文件不会被覆盖。
- 翻译用你自己配置的模型 API

## 网盘怎么接？

能通过 WebDAV 上传文件的存储，用起来最省事：生成的字幕可以直接放回视频旁边。

比如 PikPak，官方目前支持 Premium 用户开启 WebDAV，并提供上传等文件管理功能。在设置里开启 WebDAV、创建key，再把服务器地址和账号密码填进 Cue 即可。[PikPak 官方说明](https://mypikpak.com/en-US/help-center/connected_apps/webdav/how_to)

阿里云盘、百度网盘、OneDrive、Google Drive 等，也可以通过 OpenList 转成 WebDAV 接入；需要给对应账号开启 WebDAV 写入和上传权限，具体以官方支持为准。[OpenList 支持列表](https://openlistteam.github.io/docs/guide/webdav.html)

Real-Debrid 这类来源，则更适合把字幕保存到本地，再通过支持加载外部 SRT 的播放器打开。
或者可以借助 [zurg](https://github.com/debridmediamanager/zurg-testing)之类的solution挂载到本地。[zurg 配置说明](https://notes.debridmediamanager.com/reference/config/)

## 目前的局限性

Cue依赖已有字幕，不包含语音转文字。如果完全找不到字幕，需要从音频开始生成，这个工具就不适用了，你可以看看 [Buzz](https://github.com/chidiwilliams/buzz)：基于 Whisper，支持本地转录和导出 SRT。

另外，**当前版本在需要校准 WebDAV 视频的时间轴时，会先下载一小段视频到内存**，再做同步。找到可直接使用的精确匹配字幕时，会跳过这一步。

安装后跟着设置向导，配好视频来源、OpenSubtitles 下载凭据和翻译 API，就可以开始用了。


项目地址：[jxu-dev-c/Cue](https://github.com/jxu-dev-c/Cue)
可以把这个文件扔给agent让它帮你安装:

如果你也喜欢双语字幕，又不想每次看剧前先折腾半天，欢迎试试+提意见。
