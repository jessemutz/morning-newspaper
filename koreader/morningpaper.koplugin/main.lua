--[[--
Morning Paper: syncs a KOReader device with a morning-paper server (scripts/paper_server.py).

  * On closing a book or going to sleep: snapshot reading progress (and the last two days of
    reading statistics) into a local outbox; send it when online.
  * On wake / Wi-Fi connected: send the outbox, fetch the manifest, download new editions
    and shelf books, prune old editions.

Settings live in <settings dir>/morningpaper.lua.
--]]

local DataStorage = require("datastorage")
local Device = require("device")
local InfoMessage = require("ui/widget/infomessage")
local InputDialog = require("ui/widget/inputdialog")
local LuaSettings = require("luasettings")
local NetworkMgr = require("ui/network/manager")
local UIManager = require("ui/uimanager")
local WidgetContainer = require("ui/widget/container/widgetcontainer")
local lfs = require("libs/libkoreader-lfs")
local logger = require("logger")
local util = require("util")
local _ = require("gettext")
local T = require("ffi/util").template

-- extend() is the modern API; older KOReader builds only have new().
local MorningPaper = (WidgetContainer.extend or WidgetContainer.new)(WidgetContainer, {
    name = "morningpaper",
    is_doc_only = false,
})

local AUTO_SYNC_MIN_GAP = 2 * 3600 -- seconds between automatic syncs on wake

-- KOReader ships rapidjson; some builds also have a "json" module. Use whichever loads.
local JSON
local function json_lib()
    if not JSON then
        local ok, lib = pcall(require, "rapidjson")
        if not ok then ok, lib = pcall(require, "json") end
        JSON = ok and lib or nil
    end
    return JSON
end
local function json_encode(v) return json_lib().encode(v) end
local function json_decode(s)
    local lib = json_lib()
    if not lib or not s then return nil end
    local ok, v = pcall(lib.decode, s)
    if ok then return v end
end

function MorningPaper:init()
    self.settings = LuaSettings:open(DataStorage:getSettingsDir() .. "/morningpaper.lua")
    self.outbox_path = DataStorage:getSettingsDir() .. "/morningpaper_outbox.json"
    self.ui.menu:registerToMainMenu(self)
end

-- ---------------------------------------------------------------------------------------
-- settings helpers

function MorningPaper:server()
    local url = (self.settings:readSetting("server") or ""):gsub("/+$", "")
    return url
end

function MorningPaper:setStatus(ok, msg)
    self.settings:saveSetting("last_result", { ok = ok, msg = msg, time = os.time() })
    self.settings:flush()
end
function MorningPaper:token() return self.settings:readSetting("token") or "" end

function MorningPaper:docRoot()
    local home = G_reader_settings:readSetting("home_dir")
    if home and lfs.attributes(home, "mode") == "directory" then return home end
    if Device:isKindle() then return "/mnt/us/documents" end
    return DataStorage:getDataDir()
end

local function notify(text, timeout)
    UIManager:show(InfoMessage:new{ text = text, timeout = timeout or 3 })
end

-- ---------------------------------------------------------------------------------------
-- HTTP

function MorningPaper:request(method, path, body, sink)
    local http = require("socket.http")
    local ltn12 = require("ltn12")
    local socket = require("socket")
    local socketutil = require("socketutil")
    local resp = {}
    local headers = { ["Authorization"] = "Bearer " .. self:token() }
    if body then
        headers["Content-Type"] = "application/json"
        headers["Content-Length"] = tostring(#body)
    end
    socketutil:set_timeout(socketutil.FILE_BLOCK_TIMEOUT, socketutil.FILE_TOTAL_TIMEOUT)
    local ok, code, rheaders = pcall(function()
        return socket.skip(1, http.request{
            url = self:server() .. path,
            method = method,
            headers = headers,
            source = body and ltn12.source.string(body) or nil,
            sink = sink or ltn12.sink.table(resp),
        })
    end)
    socketutil:reset_timeout()
    if not ok then
        logger.warn("MorningPaper: request failed", path, code)
        return nil
    end
    return code, table.concat(resp), type(rheaders) == "table" and rheaders or {}
end

-- ---------------------------------------------------------------------------------------
-- progress snapshot

function MorningPaper:currentBook()
    local ui = self.ui
    if not (ui and ui.document and ui.document.file) then return nil end
    local file = ui.document.file
    local md5 = ui.doc_settings and ui.doc_settings:readSetting("partial_md5_checksum")
    if not md5 then md5 = util.partialMD5(file) end
    local page, pages
    pcall(function()
        page = ui:getCurrentPage()
        pages = ui.document:getPageCount()
    end)
    local percent
    if page and pages and pages > 0 then
        percent = page / pages
    elseif ui.doc_settings then
        percent = ui.doc_settings:readSetting("percent_finished")
    end
    local props = ui.doc_props or (ui.document.getProps and ui.document:getProps()) or {}
    local chapter
    pcall(function() chapter = ui.toc:getTocTitleByPage(page) end)
    return {
        md5 = md5,
        file = file,
        title = props.display_title or props.title,
        authors = props.authors,
        percent = percent,
        page = page,
        pages = pages,
        chapter = chapter,
        updated = os.time(),
    }
end

function MorningPaper:recentStats()
    local db = DataStorage:getSettingsDir() .. "/statistics.sqlite3"
    if lfs.attributes(db, "mode") ~= "file" then return {} end
    local out = {}
    local ok, err = pcall(function()
        local SQ3 = require("lua-ljsqlite3/init")
        local conn = SQ3.open(db)
        local since = os.time() - 2 * 86400
        local res = conn:exec(string.format([[
            SELECT b.md5, b.title, date(p.start_time, 'unixepoch', 'localtime') AS d,
                   count(DISTINCT p.page), sum(p.duration)
            FROM page_stat_data p JOIN book b ON b.id = p.id_book
            WHERE p.start_time > %d
            GROUP BY b.md5, d]], since))
        conn:close()
        if res and res[1] then
            for i = 1, #res[1] do
                table.insert(out, {
                    md5 = res[1][i], title = res[2][i], date = res[3][i],
                    pages = tonumber(res[4][i]), seconds = tonumber(res[5][i]),
                })
            end
        end
    end)
    if not ok then logger.warn("MorningPaper: stats query failed", err) end
    return out
end

function MorningPaper:snapshot()
    local book = self:currentBook()
    if not book then return end
    local f = io.open(self.outbox_path, "r")
    local outbox = f and json_decode(f:read("*a")) or {}
    if f then f:close() end
    outbox.books = outbox.books or {}
    outbox.books[book.md5] = book -- latest per book
    f = io.open(self.outbox_path, "w")
    if f then
        f:write(json_encode(outbox))
        f:close()
    end
end

function MorningPaper:flushOutbox()
    local f = io.open(self.outbox_path, "r")
    local outbox = f and json_decode(f:read("*a")) or { books = {} }
    if f then f:close() end
    local books = {}
    for _k, b in pairs(outbox.books or {}) do table.insert(books, b) end
    local body = json_encode{
        device = Device.model or "koreader",
        sent = os.time(),
        books = books,
        stats = self:recentStats(),
    }
    local code = self:request("POST", "/api/progress", body)
    if code == 200 then
        os.remove(self.outbox_path)
        return true
    end
    logger.warn("MorningPaper: progress upload failed", code)
    return false
end

-- ---------------------------------------------------------------------------------------
-- downloads

function MorningPaper:download(item, dest)
    local ltn12 = require("ltn12")
    local dir = self:docRoot() .. "/" .. item.folder
    util.makePath(dir)
    dest = dest or (dir .. "/" .. item.filename)
    local tmp = dest .. ".part"
    -- Escape spaces etc. ourselves too, in case an older server sends a raw path.
    local url = item.url:gsub("[^%w%-%._~/%%]", function(c) return string.format("%%%02X", c:byte()) end)
    local why
    for attempt = 1, 3 do -- Kindle Wi-Fi drops transfers now and then (often right after wake)
        local fh = io.open(tmp, "wb")
        if not fh then return false, T(_("%1: cannot write %2"), item.filename, tmp) end
        local started = os.time()
        local code, _body, headers = self:request("GET", url, nil, ltn12.sink.file(fh))
        pcall(function() fh:close() end) -- the sink only closes (and flushes) on a clean end
        local size = lfs.attributes(tmp, "size")
        if code == 200 and size == item.size then
            os.remove(dest) -- some filesystems will not rename over an existing file
            local ok, rerr = os.rename(tmp, dest)
            if ok and lfs.attributes(dest, "size") == item.size then
                logger.info("MorningPaper: saved", dest)
                return dest
            end
            os.remove(tmp)
            return false, T(_("%1: downloaded but could not save as %2 (%3)"), item.filename, dest,
                tostring(rerr))
        end
        os.remove(tmp)
        local secs = os.time() - started
        why = T(_("%1: HTTP %2, %3 of %4 bytes (server sent length %5) after %6 s, %7 tries"),
            item.filename, tostring(code or "no connection"), tostring(size or 0), tostring(item.size),
            tostring(headers and headers["content-length"] or "?"), tostring(secs), tostring(attempt))
        logger.warn("MorningPaper: download failed", url, why)
    end
    return false, why
end

function MorningPaper:syncFiles()
    local code, body = self:request("GET", "/api/manifest")
    if code == 401 then return nil, _("Server rejected the access token.") end
    if code ~= 200 then
        return nil, T(_("Could not reach %1 (%2)."), self:server(), tostring(code or "no connection"))
    end
    local manifest = json_decode(body)
    if not manifest then return nil, _("Bad manifest.") end
    local have = self.settings:readSetting("downloaded") or {}
    local wanted, new_editions, new_books, newest = {}, 0, 0, nil
    local failures = {}
    for _k, item in ipairs(manifest.items or {}) do
        wanted[item.id] = item
        local dest = self:docRoot() .. "/" .. item.folder .. "/" .. item.filename
        local prev = have[item.id]
        -- A book keeps the file it was first saved as, even if its title changes in the
        -- library: same path = same KOReader progress, highlights and sidecar.
        if item.kind == "book" and prev and prev.verified and prev.path then dest = prev.path end
        local present = lfs.attributes(dest, "mode") == "file"
        -- Editions are always kept current. A book is updated when the library copy changes
        -- (metadata edited in CWA/Calibre), but one you deleted on the device stays deleted.
        local skip
        if item.kind == "book" then
            -- A missing file counts as "deleted on purpose" only if we saw it saved. Records
            -- from older plugin versions were sometimes written for files that never landed.
            skip = prev and ((prev.sha256 == item.sha256 and present) or (not present and prev.verified))
        else
            skip = prev and prev.sha256 == item.sha256 and present
        end
        if not skip then
            local path, why = self:download(item, dest)
            if not path then
                table.insert(failures, why or item.filename)
            elseif path then
                have[item.id] = { sha256 = item.sha256, path = path, kind = item.kind, verified = true }
                if item.kind == "edition" then
                    new_editions = new_editions + 1
                    if not newest or path > newest then newest = path end
                else
                    new_books = new_books + 1
                end
            end
        end
    end
    -- Prune editions that fell out of the manifest (never books).
    for id, rec in pairs(have) do
        if rec.kind == "edition" and not wanted[id] then
            if rec.path and lfs.attributes(rec.path, "mode") == "file" then
                os.remove(rec.path)
                pcall(function()
                    local DocSettings = require("docsettings")
                    require("ffi/util").purgeDir(DocSettings:getSidecarDir(rec.path))
                end)
            end
            have[id] = nil
        end
    end
    self.settings:saveSetting("downloaded", have)
    -- Latest edition = newest edition file actually on the device, downloaded now or before.
    for _k, rec in pairs(have) do
        if rec.kind == "edition" and rec.path and lfs.attributes(rec.path, "mode") == "file"
                and (not newest or rec.path > newest) then
            newest = rec.path
        end
    end
    if newest then self.settings:saveSetting("latest_edition", newest) end
    self.settings:flush()
    if new_editions + new_books > 0 then
        local FileManager = require("apps/filemanager/filemanager")
        if FileManager.instance then FileManager.instance:onRefresh() end
    end
    if #failures > 0 then
        return nil, T(_("%1 download(s) failed:\n%2"), #failures, table.concat(failures, "\n"))
    end
    return { editions = new_editions, books = new_books }
end

function MorningPaper:sync(interactive)
    if self:server() == "" or self:token() == "" then
        if interactive then notify(_("Set the server URL and token first.")) end
        return
    end
    if self.syncing then return end
    self.syncing = true
    self:snapshot()
    local sent = self:flushOutbox()
    local res, err = self:syncFiles()
    self.syncing = false
    if not res then
        self:setStatus(false, err or _("Sync failed."))
        if interactive then notify(err or _("Sync failed.")) end
        return
    end
    self:setStatus(true, T(_("%1 new edition(s), %2 new book(s); progress %3."),
        res.editions, res.books, sent and _("sent") or _("not sent")))
    self.settings:saveSetting("last_sync", os.time())
    self.settings:flush()
    if res.editions > 0 then
        notify(_("Today's Morning Paper is ready."))
    elseif res.books > 0 then
        notify(T(_("%1 new book(s) from your library."), res.books))
    elseif interactive then
        notify(_("Morning Paper: up to date."))
    end
end

-- ---------------------------------------------------------------------------------------
-- events

function MorningPaper:onCloseDocument()
    self:snapshot()
end

function MorningPaper:onSuspend()
    self:snapshot()
    -- Best effort: if Wi-Fi is still up, send it now so tonight's reading reaches the
    -- morning build. Otherwise it goes on the next wake.
    if NetworkMgr:isConnected() and self:server() ~= "" then
        pcall(function() self:flushOutbox() end)
    end
end

function MorningPaper:autoSyncDue()
    if not self.settings:nilOrTrue("auto_sync") then return false end
    local last = self.settings:readSetting("last_sync") or 0
    return os.time() - last >= AUTO_SYNC_MIN_GAP
end

function MorningPaper:onResume()
    if not self:autoSyncDue() then return end
    -- Give the Kindle a few seconds to bring Wi-Fi back after wake.
    UIManager:scheduleIn(5, function()
        if NetworkMgr:isOnline() then
            self:sync(false)
        elseif self.settings:isTrue("wifi_on_wake") then
            NetworkMgr:runWhenOnline(function() self:sync(false) end)
        end
    end)
end

function MorningPaper:onNetworkConnected()
    if self:autoSyncDue() then
        UIManager:nextTick(function() self:sync(false) end)
    end
end

-- ---------------------------------------------------------------------------------------
-- menu

function MorningPaper:editSetting(key, title, hint)
    local dlg
    dlg = InputDialog:new{
        title = title,
        input = self.settings:readSetting(key) or "",
        input_hint = hint,
        buttons = {{
            { text = _("Cancel"), id = "close", callback = function() UIManager:close(dlg) end },
            { text = _("Save"), is_enter_default = true, callback = function()
                self.settings:saveSetting(key, util.trim(dlg:getInputText()))
                self.settings:flush()
                UIManager:close(dlg)
            end },
        }},
    }
    UIManager:show(dlg)
    dlg:onShowKeyboard()
end

function MorningPaper:openLatest()
    local p = self.settings:readSetting("latest_edition")
    if not (p and lfs.attributes(p, "mode") == "file") then
        return notify(_("No edition downloaded yet. Use Sync now."))
    end
    UIManager:nextTick(function()
        if self.ui.document and self.ui.switchDocument then
            self.ui:switchDocument(p)  -- already reading something: swap books
        else
            require("apps/reader/readerui"):showReader(p)
        end
    end)
end

local function fmt_time(t)
    if not t or t == 0 then return _("never") end
    return os.date("%Y-%m-%d %H:%M", t)
end

function MorningPaper:pendingCount()
    local f = io.open(self.outbox_path, "r")
    if not f then return 0 end
    local outbox = json_decode(f:read("*a")) or {}
    f:close()
    local n = 0
    for _k in pairs(outbox.books or {}) do n = n + 1 end
    return n
end

function MorningPaper:showStatus()
    local r = self.settings:readSetting("last_result") or {}
    local latest = self.settings:readSetting("latest_edition")
    local lines = {
        T(_("Server: %1"), self:server() ~= "" and self:server() or _("not set")),
        T(_("Token: %1"), self:token() ~= "" and _("set") or _("not set")),
        T(_("Auto sync on wake: %1"), self.settings:nilOrTrue("auto_sync") and _("on") or _("paused")),
        T(_("Last successful sync: %1"), fmt_time(self.settings:readSetting("last_sync"))),
        T(_("Last attempt: %1 (%2)"), fmt_time(r.time), r.ok == nil and "-" or (r.ok and _("ok") or _("failed"))),
        r.msg and ("  " .. r.msg) or nil,
        T(_("Progress waiting to send: %1 book(s)"), self:pendingCount()),
        T(_("Latest edition: %1"), latest and latest:match("[^/]+$") or _("none yet")),
        T(_("Download folder: %1"), self:docRoot()),
    }
    local text = {}
    for _k, l in ipairs(lines) do if l then table.insert(text, l) end end
    UIManager:show(InfoMessage:new{ text = table.concat(text, "\n") })
end

function MorningPaper:checkConnection()
    if self:server() == "" then return notify(_("Set the server URL first.")) end
    local code = self:request("GET", "/health")
    if code ~= 200 then
        return notify(T(_("Cannot reach %1 (%2)."), self:server(), tostring(code or "no connection")), 6)
    end
    local mcode, body = self:request("GET", "/api/manifest")
    if mcode == 401 then return notify(_("Server is up, but the access token is wrong."), 6) end
    local m = mcode == 200 and json_decode(body) or nil
    local eds, books = 0, 0
    for _k, it in ipairs(m and m.items or {}) do
        if it.kind == "edition" then eds = eds + 1 else books = books + 1 end
    end
    notify(T(_("Connected. Server has %1 edition(s) and %2 shelf book(s)."), eds, books), 6)
end

function MorningPaper:redownloadBooks()
    local have = self.settings:readSetting("downloaded") or {}
    for id, rec in pairs(have) do
        if rec.kind == "book" then
            logger.info("MorningPaper: forgetting", id, rec.path)
            have[id] = nil
        end
    end
    self.settings:saveSetting("downloaded", have)
    self.settings:flush()
    NetworkMgr:runWhenOnline(function() self:sync(true) end)
end

function MorningPaper:addToMainMenu(menu_items)
    menu_items.morningpaper = {
        text = _("Morning Paper"),
        sorting_hint = "tools",
        sub_item_table = {
            {
                text = _("Status"),
                keep_menu_open = true,
                callback = function() self:showStatus() end,
            },
            {
                text = _("Check connection"),
                keep_menu_open = true,
                callback = function()
                    NetworkMgr:runWhenOnline(function() self:checkConnection() end)
                end,
            },
            {
                text = _("Open latest edition"),
                enabled_func = function()
                    local p = self.settings:readSetting("latest_edition")
                    return p and lfs.attributes(p, "mode") == "file"
                end,
                callback = function() self:openLatest() end,
            },
            {
                text = _("Sync now"),
                callback = function()
                    NetworkMgr:runWhenOnline(function() self:sync(true) end)
                end,
            },
            {
                text = _("Re-download library books"),
                help_text = _("Forget which shelf books were already downloaded (or deleted on purpose), then sync."),
                callback = function() self:redownloadBooks() end,
            },
            {
                text = _("Sync automatically on wake (uncheck to pause)"),
                checked_func = function() return self.settings:nilOrTrue("auto_sync") end,
                callback = function()
                    self.settings:saveSetting("auto_sync", not self.settings:nilOrTrue("auto_sync"))
                    self.settings:flush()
                end,
            },
            {
                text = _("Turn on Wi-Fi to sync on wake"),
                checked_func = function() return self.settings:isTrue("wifi_on_wake") end,
                callback = function()
                    self.settings:saveSetting("wifi_on_wake", not self.settings:isTrue("wifi_on_wake"))
                    self.settings:flush()
                end,
            },
            {
                text = _("Server URL"),
                keep_menu_open = true,
                separator = false,
                callback = function()
                    self:editSetting("server", _("Morning Paper server URL"), "http://truenas.local:8765")
                end,
            },
            {
                text = _("Access token"),
                keep_menu_open = true,
                callback = function()
                    self:editSetting("token", _("Morning Paper access token"), "PAPER_TOKEN")
                end,
            },
        },
    }
end

-- A Lua error inside a menu action or event handler closes KOReader. Wrap every entry point
-- so a failure is logged (crash.log) and, for things you tapped, shown on screen instead.
local function guard(name, interactive)
    local fn = MorningPaper[name]
    MorningPaper[name] = function(self, ...)
        local ok, err = pcall(fn, self, ...)
        if not ok then
            self.syncing = false
            logger.err("MorningPaper:", name, "failed:", err)
            if interactive then
                notify(T(_("Morning Paper error in %1:\n%2"), name, tostring(err)), 10)
            end
        end
        return nil
    end
end
for _k, name in ipairs({ "onCloseDocument", "onSuspend", "onResume", "onNetworkConnected" }) do
    guard(name, false)
end
for _k, name in ipairs({ "sync", "showStatus", "checkConnection", "openLatest", "editSetting",
                           "redownloadBooks" }) do
    guard(name, true)
end

return MorningPaper
