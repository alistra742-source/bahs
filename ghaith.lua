--[[
Ghaith 2.0 -- the Roblox client for the bahs agent service (agent / qwen / deepseek).

What it is: a panel in the game that talks to POST /chat/stream and GET /chat/result/{job},
keeps the whole conversation, shows the model working while it works, and hands back a finished
Luau script.

Four things worth knowing before reading the code:

  * The service ships the script with the markdown fence already taken off. It is pasted straight
    into an executor, where a ```lua line is a syntax error, so the answer comes back bare -- a
    client that only reads inside fences finds nothing at all here. That is what extract() below
    is built around. Nothing it hands back is ever half a script: the whole answer is read, and
    the words "read the whole thing" are enforced by a balance check, not by hope.
  * Nothing goes out in halves either: the whole conversation is sent every turn, with a system
    turn in front that says how the script must come back.
  * Thinking is mentioned, never printed. The service streams the model's own chain of thought on a
    channel of its own -- the `thoughts` field of /chat/result -- and the client puts it to exactly
    one use: the status line at the foot of the transcript reads "thinking · 12s · 340 chars
    thought" while the turn is working something out, so a quiet minute does not read as broken. The
    WRITER button turns that thinking off for a turn (the service takes the setting per turn, so it
    can be either one on any turn), and the status line then reads "writing" for the same reason: it
    is writing, not working it out. The reasoning itself is never printed, in a pane or in the
    transcript: it is long, and it is not what anybody is waiting for. "copy code" never copies it
    either: the script is the only thing here that is code.
  * The model can call tools on this client by writing a token in its answer -- @@GREP remote@@ or
    @@GREP@@ remote, both are read. Twenty-nine of them, and every one of them is about the game
    this client is running in rather than about the machine it is running on: the dump, the
    remotes, every script and module the client holds (and the decompiler, for the ones it was
    never sent the source of), greps and string searches, hooks, signals and properties watched,
    players and the console, a remote fired for real, a function hooked in place, its upvalues and
    constants, the garbage collector, what this executor hands a script, and a live run. Whatever
    they found goes back as the next turn -- that is the agentic part -- and a line carrying a
    token is never part of the script.

Buttons: send (Enter), modes, WRITER (thinking or fast: the same model with the reasoning or
without it), scan game (the whole game written out and sent as the question -- every script with
its text, then every other instance in it by name and class -- shown in a window of its own),
console, errors (a console error is sent to the model with the script it
came from, for a fixed script, up to three in a row), and auto -- which runs what it wrote, hands
the console back, gets a fix and repeats until the script stops changing. copy code, run and full
are not up there: they are built under each answer that carries a script, so the rail never shows a
run button with no script to run.

On a phone as well as on a desktop. The panel fills the screen it was given -- minus the strip
Roblox keeps for its own buttons, which is where its header would otherwise be sitting -- with the
rail beside the column, or above it as a scrolling strip when the screen is too narrow to put them
side by side. It and every window it opens are dragged by their bars with a finger or with a mouse:
`Draggable`, the property that sounds like this, only ever listens to a mouse, so on a touch screen
it does not move at all.
]]

-- =====================================================================================
-- 1. what to talk to
-- =====================================================================================

local URL         = "https://bahs-production-d68f.up.railway.app"
local KEY         = "roblox321@"    -- API key, if the service asks for one
local MODE        = "agent"         -- agent (plan then write) | qwen | deepseek
local THINK       = "thinking"      -- thinking (work it out first) | fast (same model, no reasoning)
-- A turn's wall-clock is the number of model calls it takes, and the size of every prompt that
-- goes with them, so both budgets here are small on purpose: HISTORY is how much conversation the
-- service is asked to read each time (a whole script per turn adds up fast), and MAXROUNDS is how
-- many extra round trips the auto loop may spend after the answer has already arrived.
local POLL        = 0.6             -- seconds between reads of the running turn
local STALL       = 150             -- seconds of nothing new before a turn is given up on
local MAXROUNDS   = 3               -- auto: how many run-and-fix rounds it may take
local HISTORY     = 16              -- turns of conversation kept here (the service trims too)
-- scan game: the whole game written out as one question. What is bounded here is the size of that
-- question -- it is what the model is asked to read -- and never which parts of the game are
-- written down: every script with its text, then every other instance by name and class, every
-- name there is. A game bigger than the budget is cut at the end of the dump, and the summary it
-- ends with says how much did not fit, so a cut dump never reads as a whole one.
local SCAN_BUDGET = 300000          -- characters of the whole dump one scan may carry
local SCAN_SOURCE = 40000           -- characters of one script's source written into it

local auto        = false           -- auto: run what it wrote, hand the console back, fix and repeat
local last_code   = ""              -- the script from the last answer, whole
local last_answer = ""              -- everything the last turn said

-- A console error is the one thing here nobody asked for, and the only place the game says that
-- something is wrong: the model wrote a script, it threw three minutes later, and the console is
-- where that shows up. It is worth a turn of its own -- the error goes to the model as the
-- question, the script that produced it is already the newest assistant turn of the same chat, and
-- the answer is the fixed script, read exactly as any other answer is. On by default, turned off by
-- the button on the rail, and bounded: three in a row and it stops and says so rather than spending
-- the conversation on a script that fails on every frame.
local watch_errors = true           -- errors: ON | off -- send a console error to the model for a fix
local ERR_FIX_MAX  = 3              -- how many console errors in a row may each spend a turn
local ERR_FIX_WAIT = 6              -- seconds a fix turn is given before another error starts one
local error_fix    = nil            -- filled in below, once the turn it needs exists
local error_fix_rounds = 0          -- how many of those three are used; a clean run puts it back to 0
local last_error, last_error_at = "", 0

-- The standing instruction. It goes in one system turn, once, instead of in front of every
-- question: it is about how the script must come back, and it is true for every question.
local SYSTEM = table.concat({
	"You are writing Luau for a Roblox executor, and a client is pasting your answer straight into it.",
	"Reply with ONLY the complete finished script: no markdown fences, no commentary, no explanation,",
	'no "here is", no "...the rest is unchanged". Even when one line changed, send the WHOLE script.',
	"Nothing before it, nothing after it.",
	"Work it out before you write: your reasoning is streamed to the user as you go, so think in the",
	"open. The script is the only thing they keep, so a whole one beats a note about one.",
}, " ")

-- =====================================================================================
-- 2. services and the executor's own primitives
-- =====================================================================================

local HS = game:GetService("HttpService")
local P  = game:GetService("Players")
local LS = game:GetService("LogService")
local TW = game:GetService("TweenService")
local UIS = game:GetService("UserInputService")   -- dragging, and knowing a finger from a mouse
local GUIS = game:GetService("GuiService")        -- the topbar strip the panel must stay out of
local LP = P.LocalPlayer
local PG = LP:WaitForChild("PlayerGui")

local HTTP = http_request or request                   -- executor HTTP, in its many shapes
	or (syn and syn.request) or (http and http.request) or (fluxus and fluxus.request)
local LOAD = loadstring or load                        -- running a script needs one of these

local function clipboard(text)
	local tries = {
		function() setclipboard(text) end, function() toclipboard(text) end,
		function() set_clipboard(text) end, function() writeclipboard(text) end,
	}
	for _, write in ipairs(tries) do
		if pcall(write) then return true end
	end
	return false
end

-- A primitive the executor may or may not hand a script, read without assuming it is there:
-- some executors put their functions in _G, some only in getgenv()'s table, and a script that
-- indexes a name that is not there is an error rather than a nil.
local function primitive(name)
	local ok, value = pcall(function() return _G[name] end)
	if ok and value ~= nil then return value end
	local env = (type(getgenv) == "function") and getgenv() or nil
	return type(env) == "table" and env[name] or nil
end

local function headers()
	local h = {["Content-Type"] = "application/json"}
	if KEY ~= "" then h["X-API-Key"] = KEY end
	return h
end

-- The panel's own elements, filled in at the bottom of the file: the turn writes into this table
-- and never holds the objects, which is also what lets the HTTP helpers below report a retry before
-- there is any panel to report it on.
local UI = {}

local function raw_http(method, path, body)
	local opts = {Url = URL .. path, Method = method, Headers = headers()}
	if body ~= nil then opts.Body = HS:JSONEncode(body) end
	-- What went wrong is kept, not swallowed: an executor that refuses the request ("Http requests
	-- are not enabled", a trust check, a sandbox rule) says so in its own words, and those words are
	-- the only clue there is when nothing answered at all.
	local why = ""
	if HTTP then
		local ok, res = pcall(HTTP, opts)
		if ok and type(res) == "table" then
			return tonumber(res.StatusCode) or 0, tostring(res.Body or "")
		end
		why = (not ok) and tostring(res) or "the executor's request function answered nothing"
	end
	-- An executor without a request function of its own still has HttpService.
	local ok, res = pcall(function()
		return HS:RequestAsync({Url = opts.Url, Method = opts.Method, Headers = opts.Headers, Body = opts.Body})
	end)
	if ok and res then return res.StatusCode, res.Body end
	if not ok then why = tostring(res) end
	return 0, (why ~= "" and why or "no HTTP is available in this executor")
end

-- ok, table-or-error. Every call on the service goes through here, so nothing else has to
-- remember which executor it is running in.
local function api(method, path, body)
	local code, raw = raw_http(method, path, body)
	if code == 0 then return false, raw end
	local ok, data = pcall(function() return HS:JSONDecode(raw) end)
	local where = method .. " " .. path
	if not ok or type(data) ~= "table" then
		-- Not JSON at all: an error page from something standing in front of the service, and its
		-- own words are the only thing there is to go on.
		return false, "HTTP " .. code .. " from " .. where
			.. (raw ~= "" and (": " .. raw:gsub("%s+", " "):sub(1, 160)) or ": empty answer")
	end
	if code >= 400 then
		-- The service always names its own failures, so no name at all means the answer did not
		-- come from the service: a proxy, a cold start, or a container being restarted.
		local detail = data.detail or data.error
		if detail == nil or detail == "" then
			detail = "HTTP " .. code .. " from " .. where
				.. " -- the answer gave no reason of its own, which the service gives for every failure"
				.. " it decides, so this came from in front of it: a cold start, a restart, or a proxy"
		end
		return false, tostring(detail)
	end
	return true, data
end

-- One request, retried while the failure is the kind that fixes itself. A 502/503/504 is a proxy, a
-- cold start or a restart in front of the service, and the same request a moment later is usually
-- served -- which is exactly what a first request after the app has been asleep looks like, and a
-- cold start can take ten seconds. Four tries, two seconds apart each time, covers that.
--
-- What is *not* retried is a 4xx (the service decided, and it would decide the same way again) and a
-- request that never got an answer at all: retrying a POST into a black hole is how one turn ends up
-- running twice.
local function api_retry(method, path, body, tries)
	tries = tries or 4
	local last = ""
	for attempt = 1, tries do
		local ok, data = api(method, path, body)
		if ok then return true, data end
		last = tostring(data)
		if attempt == tries or not last:find("^HTTP 5%d%d") then break end
		UI.setStatus("retrying", "the service answered " .. last:sub(1, 48) .. " -- try "
			.. (attempt + 1) .. " of " .. tries)
		task.wait(2 * attempt)
	end
	return false, last
end

-- =====================================================================================
-- 3. the console this client can show the model
-- =====================================================================================

local LOGS = {}

-- Whether a line that reached the console is a failure rather than a message. Roblox hands
-- LogService an Enum.MessageType of its own for the errors it raises, so for those the kind answers
-- it; a `print` cannot say it that way, so the shape Roblox writes an error in is read too --
-- `Script:12: attempt to index nil with 'Health'`, and the traceback printed under it.
local ERROR_LINE = "^[%w_%.]+:%d+:"
local function looks_like_error(kind, text)
	if tostring(kind or ""):lower():find("error", 1, true) then return true end
	local body = tostring(text or "")
	if body:find("stack traceback", 1, true) then return true end
	return body:find(ERROR_LINE) ~= nil
end

local function log(kind, text)
	table.insert(LOGS, {kind = kind, text = tostring(text), at = os.date("%H:%M:%S")})
	while #LOGS > 300 do table.remove(LOGS, 1) end
	if UI.onLog then UI.onLog(LOGS[#LOGS]) end
	-- An error is handed to the turn below the moment it arrives; a message is just logged. What
	-- `error_fix` is nil means is that this panel is still being built, which is why a line printed
	-- before it exists starts nothing.
	if watch_errors and error_fix and looks_like_error(kind, text) then
		task.spawn(error_fix, tostring(text or ""))
	end
end

pcall(function()
	LS.MessageOut:Connect(function(message, kind) log(tostring(kind), message) end)
end)

local real_print = print
print = function(...)
	local parts = {}
	for i = 1, select("#", ...) do parts[i] = tostring((select(i, ...))) end
	log("Print", table.concat(parts, "  "))
	return real_print(...)
end

local real_warn = warn
warn = function(...)
	local parts = {}
	for i = 1, select("#", ...) do parts[i] = tostring((select(i, ...))) end
	log("Warn", table.concat(parts, "  "))
	return real_warn(...)
end

local function console_since(mark, limit)
	local out = {}
	for i = mark + 1, #LOGS do
		local line = LOGS[i]
		table.insert(out, "[" .. line.kind .. "] " .. line.text)
	end
	local text = table.concat(out, "\n")
	if limit and #text > limit then text = text:sub(-limit) end
	return text
end

-- =====================================================================================
-- 4. reading the script back out of an answer
-- =====================================================================================

-- One word, whole-word only: `end` matches in `end)` but not in `weekend`.
local function words(line, w)
	local n = 0
	for _ in line:gmatch("%f[%a]" .. w .. "%f[%A]") do n = n + 1 end
	return n
end

-- Block balance, counted line by line so the `do` of a `for`/`while` header is not counted a
-- second time: `for i = 1, 10 do ... end` is one block, not two. A trailing comment is cut
-- first, so `-- end of the loop` does not read as a closer.
local function balance(c)
	local open, close = 0, 0
	for line in ((c or "") .. "\n"):gmatch("([^\n]*)\n") do
		line = line:gsub("%-%-.*$", "")
		local forw, whilew = words(line, "for"), words(line, "while")
		local dos = words(line, "do")
		if dos > 0 and (forw + whilew) > 0 then dos = math.max(0, dos - math.min(dos, forw + whilew)) end
		open = open + words(line, "function") + words(line, "if") + forw + whilew + dos + words(line, "repeat")
		close = close + words(line, "end") + words(line, "until")
	end
	return open - close
end

-- A line that reads as code rather than as a sentence about code. Deliberately generous: a run
-- of code is cut by the first line this says no to, so assignments, closers and table rows all
-- have to count.
local function is_code(l)
	local x = (l or ""):gsub("^%s+", ""):gsub("%s+$", "")
	if x == "" then return false end
	if x:find("^%-%-") then return true end
	if x:find("^end[%s%)%,;]*$") then return true end
	if x:find("^[%)%}%],;]+$") then return true end
	if x:find("^[%[%{]") then return true end
	if x:find("^else") or x:find("^until") then return true end
	if x:find("^local%s") or x:find("^function%s") or x:find("^if%s") or x:find("^for%s")
		or x:find("^while%s") or x:find("^repeat%s") or x:find("^return") or x:find("^break")
		or x:find("^continue") then return true end
	if x:find("^game[%.:]") or x:find("^Instance%.") or x:find("^task%.") or x:find("^require%s*%(")
		or x:find("^pcall%s*%(") or x:find("^spawn%s*%(") or x:find("^wait%s*%(")
		or x:find("^print%s*%(") or x:find("^warn%s*%(") then return true end
	if x:find("^workspace[%.:]") or x:find("^script[%.:]") or x:find("^table%.")
		or x:find("^string%.") or x:find("^math%.") or x:find("^coroutine%.") then return true end
	if x:find("^[%a_][%w_%.%[%]:]*%s*=") then return true end
	if x:find("^[%a_][%w_%.%[%]:]*%s*[%.:]%s*[%a_][%w_]*%s*%(") then return true end
	if x:find("^[%a_][%w_%.%:]*%s*%(") then return true end
	return false
end

-- Things a sentence about a script says and a script almost never does. Only ever used to decide
-- whether an answer IS the script; nothing here edits one. Checked only on the lines that do not
-- read as code, so `humanoid.Changed:Connect(f)` cannot be mistaken for the word "Changed".
local TALK = {
	"we should", "let me", "here's", "here is", "hope this", "feel free", "i'll write",
	"i will write", "we'll", "you can", "you should", "note that", "the rest", "unchanged",
	"same as", "step 1", "step 2", "explanation", "changed", "added", "removed", "new version",
	"only the changed",
}

local function has_talk(c)
	for line in ((c or "") .. "\n"):gmatch("([^\n]*)\n") do
		if line:find("%S") and not is_code(line) then
			local lower = line:lower()
			for _, phrase in ipairs(TALK) do
				if lower:find(phrase, 1, true) then return true end
			end
		end
	end
	return false
end

-- Whether an answer IS one script: long enough, structurally whole (one stray block of slack),
-- mostly code lines, and free of tell-tale talk.
local function looks_like_script(c)
	local b = (c or ""):gsub("^%s+", ""):gsub("%s+$", "")
	if #b < 80 then return false end
	if math.abs(balance(b)) > 1 then return false end
	if has_talk(b) then return false end
	local lines, code = 0, 0
	for line in (b .. "\n"):gmatch("([^\n]*)\n") do
		if line:find("%S") then
			lines = lines + 1
			if is_code(line) then code = code + 1 end
		end
	end
	if lines < 3 then return false end
	return code * 3 >= lines * 2
end

-- Any line carrying a tool token is not part of a script: `@@` is not Luau, so a line that has one
-- is a call rather than code, and leaving it in would paste a token into the executor. Declared
-- here because everything below reads an answer through extract().
local function strip_tool_lines(text)
	if not text or text == "" then return text or "" end
	local out = {}
	for line in ((text .. "\n"):gmatch("([^\n]*)\n")) do
		if not line:find("@@[A-Z_]+[%s@]") and not line:find("@@[A-Z_]+$") then
			table.insert(out, line)
		end
	end
	return table.concat(out, "\n")
end

-- Whether every line of it reads as code: the test looks_like_script makes, without the length
-- floor, for the short scripts that test would turn down. What it is for is deciding whether a
-- short answer may be treated as code at all.
local function all_code(c)
	local lines, code = 0, 0
	for line in ((c or "") .. "\n"):gmatch("([^\n]*)\n") do
		if line:find("%S") then
			lines = lines + 1
			if is_code(line) then code = code + 1 end
		end
	end
	return lines > 0 and code * 3 >= lines * 2 and not has_talk(c)
end

-- The script out of an answer. In order: a fenced block if one is there (a model that fenced
-- anyway); the whole answer when it reads as one script (the normal path -- this is what the
-- service sends); otherwise the longest run of lines that read as code; and last the whole answer
-- again when every line of it reads as code, which is how a one-line script is read at all. ""
-- means the answer carried no script, which is what the re-ask keys off.
local function extract(text)
	if not text or text == "" then return "" end
	local t = strip_tool_lines(text:gsub("\r\n", "\n"):gsub("\r", "\n"))

	local best, i = "", 1
	while true do
		local a = t:find("```", i, true)
		if not a then break end
		local z = t:find("```", a + 3, true)
		if not z then break end
		local c = t:sub(a + 3, z - 1)
		local tag, rest = c:match("^([^\n]-)\n(.*)$")      -- a language tag on the fence line
		if tag and #tag <= 16 and tag:find("%a") and not tag:find("[%s%(%)%[%]{};=]") then c = rest end
		c = c:gsub("^%s+", ""):gsub("%s+$", "")
		if #c >= 80 and #c > #best then best = c end
		i = z + 3
	end
	if #best >= 80 then return best end

	local whole = t:gsub("^%s+", ""):gsub("%s+$", "")
	if looks_like_script(whole) then return whole end

	local L = {}
	for line in (t .. "\n"):gmatch("([^\n]*)\n") do L[#L + 1] = line end
	local start, span, run_start, run_lines, last = 0, 0, 0, 0, 0
	for ix = 1, #L do
		if is_code(L[ix]) then
			if run_start == 0 then run_start = ix end
			run_lines = run_lines + 1
			last = ix
		elseif L[ix]:find("%S") then
			-- a blank line does not end a run (scripts are full of them); a line of talk does
			if run_start > 0 and run_lines > span then start, span = run_start, last - run_start + 1 end
			run_start, run_lines = 0, 0
		end
	end
	if run_start > 0 and run_lines > span then start, span = run_start, last - run_start + 1 end
	if start > 0 and span > 3 then
		local out = {}
		for k = start, start + span - 1 do out[#out + 1] = L[k] end
		local c = table.concat(out, "\n"):gsub("^%s+", ""):gsub("%s+$", "")
		if #c >= 80 and math.abs(balance(c)) <= 1 then return c end
	end
	-- Nothing above took it, and every floor above is about cutting a script *out of* something.
	-- An answer that is nothing but code is a script however short it is: a one-line `print(...)` is
	-- three tokens and a whole script, and a turn reading it with extract alone reported no script
	-- while the SCRIPT pane -- which reads with all_code above -- was showing one. Both readers agree
	-- here rather than the turn guessing differently, so what the pane holds is what the turn ships.
	local bare = t:gsub("^%s+", ""):gsub("%s+$", "")
	if all_code(bare) then return bare end
	return ""
end

-- What a copy button actually copies: the script, and never a fence line, even if a model wrote
-- one anyway.
local function only_code(text)
	local code = extract(text)
	if code == "" then
		-- The fallback is for a script too short for extract's floor, not for prose: what is left
		-- once the tool lines come out still has to read as code, or the copier would hand a
		-- paragraph to the executor with the model's tool calls cut out of it.
		local bare = strip_tool_lines(text or "")
		if all_code(bare) then code = bare end
	end
	local out = {}
	for line in ((code .. "\n"):gmatch("([^\n]*)\n")) do
		if not line:find("^%s*```") then table.insert(out, line) end
	end
	return table.concat(out, "\n"):gsub("^%s+", ""):gsub("%s+$", "")
end

-- =====================================================================================
-- 5. running a script, and the tools the model can ask for
-- =====================================================================================

-- (ok, what it printed, or the error and the traceback)
local function run_script(code)
	if not LOAD then return false, "there is no loadstring in this executor, so nothing can run" end
	local fn, err = LOAD(code)
	if not fn then return false, "compile error: " .. tostring(err) end
	local mark = #LOGS
	local ok, result = xpcall(fn, function(e)
		local trace = debug.traceback("", 2)
		return tostring(e) .. "\n" .. tostring(trace)
	end)
	local printed = console_since(mark, 4000)
	local body = printed ~= "" and ("\nConsole:\n" .. printed) or "\nConsole: (nothing printed)"
	if ok then return true, "ran without error" .. body end
	return false, tostring(result) .. body
end

local SERVICES = {
	"Workspace", "ReplicatedStorage", "ReplicatedFirst", "ServerScriptService", "ServerStorage",
	"StarterGui", "StarterPack", "StarterPlayer", "Lighting", "SoundService", "Teams",
	"Players", "Chat", "TextChatService", "StarterPlayerScripts", "StarterCharacterScripts",
}

local function path_of(instance)
	local parts, node = {}, instance
	while node and node ~= game do
		table.insert(parts, 1, node.Name)
		node = node.Parent
	end
	return table.concat(parts, ".")
end

-- A service's name in whatever case it was typed: a model writes "workspace", the instance is
-- called "Workspace", and the two are the same service.
local SERVICE_NAME = {}
for _, name in ipairs(SERVICES) do SERVICE_NAME[name:lower()] = name end

-- The first instance below `root` called `name`, so a path written from memory that misses a link
-- can still be followed to what was meant. Case is forgiven here and nowhere else: an instance
-- called `WeaponRemote` is what `weaponremote` in a path meant.
local function descendant_named(root, name)
	local ok, list = pcall(function() return root:GetDescendants() end)
	if not ok then return nil end
	local wanted = name:lower()
	for _, item in ipairs(list) do
		local ok_named, called = pcall(function() return item.Name:lower() == wanted end)
		if ok_named and called then return item end
	end
	return nil
end

-- A path, read as the instance it means.
--
-- Accepted: `ReplicatedStorage.X`, `game.ReplicatedStorage.X`, `game:GetService("ReplicatedStorage").X`
-- -- everything here is already inside the game, so the prefix is noise -- a service name in the
-- wrong case, a path with the client's own token marks still on it, and a bare name. A path that
-- misses a link is completed by name rather than refused, and what the caller prints is the real
-- path, so the model can see where the instance actually was.
--
-- Returns the instance, or `nil, piece` naming the part of the path that was not there.
local function resolve(path)
	local cleaned = tostring(path or ""):gsub("^%s+", ""):gsub("%s+$", ""):gsub("@", "")
	cleaned = cleaned:gsub("^game%s*[%.:]%s*", "")
	local service, rest = cleaned:match("^GetService%s*%(%s*[\"']?([^\"')]+)[\"']?%s*%)%s*%.?(.*)$")
	if service then cleaned = service .. (rest ~= "" and ("." .. rest) or "") end
	cleaned = cleaned:gsub("^:?%s*", ""):gsub("%.$", "")
	if cleaned == "" then return game end
	local node = game
	for piece in cleaned:gmatch("[^%.]+") do
		piece = piece:gsub("^%s+", ""):gsub("%s+$", ""):gsub("[\"')]+$", "")
		if piece ~= "" then
			local child = node:FindFirstChild(piece)
			if not child and SERVICE_NAME[piece:lower()] then
				child = node:FindFirstChild(SERVICE_NAME[piece:lower()])
			end
			if not child then child = descendant_named(node, piece) end
			if not child and node ~= game then child = descendant_named(game, piece) end
			if not child then return nil, piece end
			node = child
		end
	end
	return node
end

local function source_of(instance, cap)
	local ok, src = pcall(function() return instance.Source end)
	if not ok or type(src) ~= "string" or #src == 0 then return nil end
	src = src:gsub("[%z\1-\8\11-\31\127]", "")
	if #src > cap then src = src:sub(1, cap) .. "\n--" end
	return src
end

-- Everything this client holds: the executor's own lists when it has them (they include instances
-- that cannot be walked to from a service -- a module required out of nowhere, a script whose
-- parent is not in the tree), and the services as well, because an executor with neither list still
-- leaves the game walkable. This is the game's own contents, not anything on the device.
local function all_instances()
	local out, seen = {}, {}
	local function add(item)
		if not item or seen[item] then return end
		seen[item] = true
		table.insert(out, item)
	end
	local function add_all(list)
		if type(list) ~= "table" then return end
		for _, item in ipairs(list) do add(item) end
	end
	for _, name in ipairs({"getinstances", "getscripts", "getloadedmodules"}) do
		local fn = primitive(name)
		if type(fn) == "function" then pcall(function() add_all(fn()) end) end
	end
	for _, name in ipairs(SERVICES) do
		local service = game:FindFirstChild(name)
		if service then
			add(service)
			local ok, list = pcall(function() return service:GetDescendants() end)
			if ok then add_all(list) end
		end
	end
	add(game)
	return out
end

local function all_scripts()
	local out = {}
	for _, item in ipairs(all_instances()) do
		local ok, is = pcall(function()
			return item:IsA("Script") or item:IsA("LocalScript") or item:IsA("ModuleScript")
		end)
		if ok and is then table.insert(out, item) end
	end
	return out
end

-- What a path that did not resolve gets back. "There is no such thing" leaves a model writing the
-- same wrong path again; the names that are there -- the ones closest to the piece that failed --
-- are what lets it write the right one, and a tool that reads the game is only useful if it can be
-- pointed at something.
local function no_such(path)
	local text = tostring(path or "")
	local piece = text:gsub("%s+$", ""):match("([^%.]+)$") or text
	local names, seen = {}, {}
	local wanted = piece:lower()
	for _, item in ipairs(all_instances()) do
		local ok, name = pcall(function() return tostring(item.Name) end)
		-- Either direction: `part` names the instance called `Part1`, and `Part1` names `part`.
		local lower = ok and tostring(name):lower() or ""
		local close = #lower >= 3 and #wanted >= 3
			and (lower:find(wanted, 1, true) or wanted:find(lower, 1, true))
		if close and not seen[name] then
			seen[name] = true
			table.insert(names, name)
		end
		if #names >= 12 then break end
	end
	if #names > 0 then
		return "there is no " .. text .. " -- names in the game closest to " .. piece .. ": "
			.. table.concat(names, ", ")
	end
	return "there is no " .. text .. " -- @@TREE path@@, @@FIND name@@ and @@DEEPSCAN@@ show what is there"
end

-- The text of one script, from wherever it can be had. `Source` first: it is the real text and it
-- costs nothing. The decompiler only when that reads empty, which is the normal case for a
-- LocalScript the client was never sent the source of -- the bytecode is all there is, and turning
-- it back into Lua is the only way to read the file at all. Nothing is invented: when neither works
-- this says so in as many words.
local function script_source(instance, cap, allow_decompile)
	local src = source_of(instance, cap)
	if src then return src, "source" end
	if not allow_decompile then return nil, "this client was not sent the source" end
	local decompile = primitive("decompile")
	if type(decompile) ~= "function" then
		return nil, "no source here, and this executor has no decompile()"
	end
	local arguments = {}
	local bytes = primitive("getscriptbytecode")
	if type(bytes) == "function" then
		local ok, raw = pcall(bytes, instance)
		if ok and type(raw) == "string" and #raw > 0 then table.insert(arguments, raw) end
	end
	table.insert(arguments, instance)
	for _, argument in ipairs(arguments) do
		local ok, text = pcall(decompile, argument)
		if ok and type(text) == "string" and #text > 0 then
			text = text:gsub("[%z\1-\8\11-\31\127]", "")
			if #text > cap then text = text:sub(1, cap) .. "\n--" end
			return text, "decompiled"
		end
	end
	return nil, "the decompiler returned nothing for it"
end

-- The whole game, written out. Two sections, in this order on purpose:
--
--   * THE SCRIPTS -- every Script, LocalScript and ModuleScript this client holds, by path, with
--     its text under it (or the line saying the client was never sent that text, and which token
--     reads it anyway);
--   * THE REST -- every other instance in the game, by class and path, grouped under the service
--     it lives in. Not a selection of the interesting classes: a part with a name is a name the
--     model can be asked about, and it is the model, not this function, that decides what matters.
--
-- The scripts come first and get the bigger share of the budget, so the part worth reading whole
-- is never the part that gets cut. What is cut is counted and said at the end, so a dump that ran
-- out of room never reads as a game that has nothing more in it. Returns the dump, and the one
-- line that says what it found.
local function deep_scan()
	local counts = {all = 0, script = 0, module = 0, remote = 0, value = 0, cut = 0}
	local script_box = {lines = {}, used = 0, cap = math.floor(SCAN_BUDGET * 0.6)}
	local name_box = {lines = {}, used = 0, cap = SCAN_BUDGET - math.floor(SCAN_BUDGET * 0.6)}

	-- Writing a line spends that box's budget and is refused once it is gone. Refused lines are
	-- counted rather than dropped quietly.
	local function write(box, line)
		line = tostring(line or "")
		if box.used + #line + 1 > box.cap then
			counts.cut = counts.cut + 1
			return false
		end
		box.used = box.used + #line + 1
		table.insert(box.lines, line)
		return true
	end

	local function is_script(instance)
		local class = instance.ClassName
		return class == "ModuleScript" or class == "Script" or class == "LocalScript"
	end

	-- The roots of the walk: every service this game actually has, whatever it is called -- a game
	-- may hold one no list of the usual names ever had -- plus the usual names in case one of them
	-- has not been created as a child yet.
	local roots, seen_root = {}, {}
	local function root(item)
		if item and not seen_root[item] then
			seen_root[item] = true
			table.insert(roots, item)
		end
	end
	local ok_children, children = pcall(function() return game:GetChildren() end)
	if ok_children then
		for _, child in ipairs(children) do root(child) end
	end
	for _, name in ipairs(SERVICES) do root(game:FindFirstChild(name)) end

	-- Read once, kept once: the walk is the slow part, and it is walked again below for the
	-- sections rather than re-read per service.
	local groups, in_group = {}, {}
	for _, node in ipairs(roots) do
		local ok, list = pcall(function() return node:GetDescendants() end)
		if ok then
			in_group[node] = true
			table.insert(groups, {root = node, list = list})
			for _, d in ipairs(list) do in_group[d] = true end
		end
	end

	-- A module required out of nowhere, or a script with no parent in the tree, is not under any
	-- service, and the walk above misses it. The executor's own lists are where those turn up, so
	-- those lists are read for it -- and only those: walking the services a second time to find
	-- what was walked a moment ago would double the slowest part of a scan for nothing.
	local strays, seen_stray = {}, {}
	local function stray(item)
		if not item or item == game or in_group[item] or seen_stray[item] then return end
		seen_stray[item] = true
		table.insert(strays, item)
	end
	for _, name in ipairs({"getinstances", "getscripts", "getloadedmodules"}) do
		local fn = primitive(name)
		if type(fn) == "function" then
			local ok_list, list = pcall(fn)
			if ok_list and type(list) == "table" then
				for _, item in ipairs(list) do stray(item) end
			end
		end
	end

	local function name_line(instance)
		if is_script(instance) then return nil end      -- written above, with its text
		counts.all = counts.all + 1
		local class = instance.ClassName
		local p = path_of(instance)
		if class == "RemoteEvent" or class == "RemoteFunction" or class == "UnreliableRemoteEvent" then
			counts.remote = counts.remote + 1
			return "[REMOTE " .. class .. "] " .. p
		end
		local ok_value, value = pcall(function() return instance:IsA("ValueBase") end)
		if ok_value and value then
			counts.value = counts.value + 1
			return "[VALUE " .. class .. "] " .. p .. " = " .. tostring(instance.Value)
		end
		return "[" .. class .. "] " .. p
	end

	-- --- the scripts, first, so they are never what a budget runs out on ---
	write(script_box, "\n== THE SCRIPTS ==")
	for _, group in ipairs(groups) do
		for _, d in ipairs(group.list) do
			if is_script(d) then
				counts.all = counts.all + 1
				local module = d.ClassName == "ModuleScript"
				if module then counts.module = counts.module + 1 else counts.script = counts.script + 1 end
				local p = path_of(d)
				write(script_box, "[" .. d.ClassName .. "] " .. p)
				local src = source_of(d, SCAN_SOURCE)
				if src then
					write(script_box, src)
				else
					write(script_box, "-- no source here: @@DECOMPILE " .. p .. "@@ reads it")
				end
			end
		end
	end
	for _, d in ipairs(strays) do
		if is_script(d) then
			counts.all = counts.all + 1
			local module = d.ClassName == "ModuleScript"
			if module then counts.module = counts.module + 1 else counts.script = counts.script + 1 end
			local p = path_of(d)
			write(script_box, "[" .. d.ClassName .. "] " .. p)
			local src = source_of(d, SCAN_SOURCE)
			if src then
				write(script_box, src)
			else
				write(script_box, "-- no source here: @@DECOMPILE " .. p .. "@@ reads it")
			end
		end
	end

	-- --- everything else, grouped by the service it lives in ---
	for _, group in ipairs(groups) do
		write(name_box, "\n== " .. tostring(group.root.Name) .. " ==")
		for _, d in ipairs(group.list) do
			local line = name_line(d)
			if line then write(name_box, line) end
		end
	end
	if #strays > 0 then
		write(name_box, "\n== held by the executor, not under a service ==")
		for _, d in ipairs(strays) do
			local line = name_line(d)
			if line then write(name_box, line) end
		end
	end

	local summary = string.format("%d instances: %d scripts, %d modules, %d remotes, %d values",
		counts.all, counts.script, counts.module, counts.remote, counts.value)
	-- Always written, budget or no budget: the counts are how a cut dump is told from a whole one.
	table.insert(name_box.lines, "\n== " .. summary .. " ==")
	if counts.cut > 0 then
		table.insert(name_box.lines, string.format(
			"== %d more lines did not fit: this dump is capped at %d characters ==",
			counts.cut, SCAN_BUDGET))
	end

	local out = {
		"GAME DUMP  " .. tostring(game.Name) .. "  place " .. tostring(game.PlaceId),
		"every script this client holds with its text, then every other instance in the game by",
		"name and class: every name there is, not a selection of them.",
	}
	for _, line in ipairs(script_box.lines) do table.insert(out, line) end
	for _, line in ipairs(name_box.lines) do table.insert(out, line) end
	return table.concat(out, "\n"), summary
end

local function remotes()
	local out = {}
	for _, d in ipairs(all_instances()) do
		if d:IsA("RemoteEvent") or d:IsA("RemoteFunction") or d:IsA("UnreliableRemoteEvent") then
			table.insert(out, "[" .. d.ClassName .. "] " .. path_of(d))
		end
	end
	if #out == 0 then return "no remotes in this client" end
	return table.concat(out, "\n")
end

-- Every script this client holds, by path, with the text of the ones whose source it has. The ones
-- it does not have are named too, and marked: a client is never sent the source of a LocalScript it
-- did not load, so `@@DECOMPILE path@@` is how those files get read.
local function sources(kind, cap)
	local out, blind, listed = {}, 0, 0
	for _, d in ipairs(all_scripts()) do
		local module = d:IsA("ModuleScript")
		if (module and (kind == "module" or kind == "all"))
			or (not module and (kind == "script" or kind == "all")) then
			listed = listed + 1
			table.insert(out, "[" .. d.ClassName .. "] " .. path_of(d))
			local src = source_of(d, cap)
			if src then
				table.insert(out, src)
			else
				blind = blind + 1
				table.insert(out, "-- no source here: @@DECOMPILE " .. path_of(d) .. "@@ reads it")
			end
		end
	end
	if listed == 0 then return "no " .. kind .. "s in this client" end
	if blind > 0 then
		table.insert(out, string.format("-- %d of the %d have no source here; @@DECOMPILE path@@ reads those",
			blind, listed))
	end
	return table.concat(out, "\n\n")
end

local function grep(word)
	if not word or word == "" then return "no word given" end
	local out, needle, blind = {}, word:lower(), 0
	for _, d in ipairs(all_scripts()) do
		local src = source_of(d, 30000)
		if not src then
			blind = blind + 1
		elseif src:lower():find(needle, 1, true) then
			table.insert(out, path_of(d))
			local shown = 0
			for line in src:gmatch("[^\n]+") do
				if line:lower():find(needle, 1, true) then
					table.insert(out, "  " .. line)
					shown = shown + 1
					if shown >= 6 then break end
				end
			end
		end
	end
	if #out == 0 then
		return "nothing in the game's readable scripts contains " .. word
			.. (blind > 0 and (" (" .. blind .. " script(s) here have no source; @@DECOMPILE path@@ reads them)")
				or "")
	end
	if blind > 0 then
		table.insert(out, "-- " .. blind .. " further script(s) were not searched: no source here")
	end
	return table.concat(out, "\n")
end

local function find_by_name(name)
	if not name or name == "" then return "no name given" end
	local out, needle = {}, name:lower()
	for _, d in ipairs(all_instances()) do
		local ok, label = pcall(function() return tostring(d.Name) end)
		if ok and label:lower():find(needle, 1, true) then
			table.insert(out, "[" .. d.ClassName .. "] " .. path_of(d))
		end
	end
	if #out == 0 then return "nothing in this client is named like " .. name end
	return table.concat(out, "\n")
end

local function tree(root)
	local node = resolve(root)
	if not node then return no_such(root) end
	local out = {}
	local function walk(item, depth)
		table.insert(out, string.rep("  ", depth) .. item.Name .. " [" .. item.ClassName .. "]")
		if depth > 5 then return end
		for _, child in ipairs(item:GetChildren()) do walk(child, depth + 1) end
	end
	walk(node, 0)
	return table.concat(out, "\n")
end

local function props(path)
	local node = resolve(path)
	if not node then return no_such(path) end
	local out = {"[PROPS] " .. path_of(node)}
	local names = {"Name", "ClassName", "Value", "Text", "Health", "MaxHealth", "WalkSpeed",
		"JumpPower", "JumpHeight", "Position", "Size", "Anchored", "CanCollide", "Transparency",
		"Enabled", "Visible", "Image", "SoundId", "UserId", "DisplayName", "Team", "Level",
		"Cash", "Money", "Coins", "Score", "Power"}
	for _, key in ipairs(names) do
		local ok, value = pcall(function() return node[key] end)
		if ok and value ~= nil then table.insert(out, key .. " = " .. tostring(value)) end
	end
	return table.concat(out, "\n")
end

-- Every string literal in the game's scripts, or the ones containing a word: the hardcoded names,
-- the remote a script compares against, the key nobody meant to ship. The filter is what turns
-- this from a dump into a search.
local function strings(word)
	local out, needle, cap = {}, (word or ""):lower(), 400
	for _, d in ipairs(all_scripts()) do
		local src = source_of(d, 30000)
		if src then
			for literal in src:gmatch('"(.-)"') do
				local keep = #literal >= 4 and #literal <= 120
				if keep and needle ~= "" then keep = literal:lower():find(needle, 1, true) ~= nil end
				if keep and #out < cap then
					table.insert(out, path_of(d) .. " -> " .. literal)
				end
			end
		end
	end
	if #out == 0 then
		return needle == "" and "no string literals in the game's scripts"
			or ("no string literal contains " .. word)
	end
	if #out >= cap then table.insert(out, "... stopped at " .. cap .. " literals") end
	return table.concat(out, "\n")
end

local HOOKED = {}
local SPY = {}

local function hook(path)
	local remote = resolve(path)
	if not remote then return no_such(path) end
	if not (remote:IsA("RemoteEvent") or remote:IsA("RemoteFunction")) then
		return path .. " is not a remote"
	end
	if HOOKED[remote] then return "already logging " .. path end
	HOOKED[remote] = true
	if remote:IsA("RemoteEvent") then
		remote.OnClientEvent:Connect(function(...)
			local args = {...}
			local parts = {}
			for i, value in ipairs(args) do parts[i] = tostring(value) end
			local line = "[FIRED] " .. path .. " <- " .. table.concat(parts, " | ")
			table.insert(SPY, line)
			log("Remote", line)
		end)
	end
	return "logging what " .. path .. " fires with"
end

local function unhook(path)
	local remote = resolve(path)
	if not remote then return no_such(path) end
	HOOKED[remote] = nil
	HOOKED[path] = nil          -- so @@HOOKFN@@ on the same path is not refused as a repeat
	return "stopped logging " .. path
end

-- The last piece of a dotted path is often a member rather than a child -- `Kick`, `FireServer`,
-- `WalkSpeed` -- so this walks everything but it, and gives back either the instance the path
-- names or the object the member lives on plus the member's name.
local function resolve_member(path)
	local object, member = game, ""
	for piece in (path or ""):gmatch("[^%.]+") do
		if member ~= "" then object = object and object:FindFirstChild(member) end
		member = piece
		if not object then return nil, nil end
	end
	if member == "" then return object, nil end
	local child = object:FindFirstChild(member)
	if child then return child, nil end
	return object, member
end

-- A written value: `5`, `true`, `"text"`, `game.Workspace.Baseplate`. Run through loadstring when
-- there is one, so a call can be fired with a real instance as an argument; the text is handed
-- over as it stands when there is not.
local function value_of(piece)
	if LOAD and piece and piece ~= "" then
		local fn = LOAD("return " .. piece)
		if fn then
			local ok, value = pcall(fn)
			if ok then return value end
		end
	end
	return piece
end

-- =====================================================================================
-- 5b. the deep end: files, remotes, functions and the garbage collector
-- =====================================================================================
--
-- Everything here reaches past the game tree, into the executor's own primitives. None of them is
-- guaranteed to exist -- executors differ -- so each one answers "this client cannot do that"
-- instead of erroring, and the tool result is what tells the model which world it is in.

-- Everything an instance fires with, by event name: the way to read what the game does to itself --
-- `@@SIGNAL workspace ChildAdded@@`, `@@SIGNAL game.Players.LocalPlayer CharacterAdded@@`. What it
-- fires with goes to @@SPY@@, so several of these can be listening at once and read together.
local function watch_signal(arg)
	local path, member = (arg or ""):match("^(%S+)%s+(%S+)$")
	if not path then
		return "use @@SIGNAL path EventName@@, e.g. @@SIGNAL game.Workspace ChildAdded@@"
	end
	local node = resolve(path)
	if not node then return no_such(path) end
	local ok, problem = pcall(function()
		local signal = node[member]
		if signal == nil then error(member .. " is not a member of " .. path) end
		signal:Connect(function(...)
			local parts = {}
			for i, value in ipairs({...}) do parts[i] = tostring(value) end
			table.insert(SPY, "[EVENT] " .. path .. "." .. member .. " -> " .. table.concat(parts, " | "))
		end)
	end)
	if not ok then
		return "could not listen to " .. path .. "." .. member .. ": " .. tostring(problem)
	end
	return "listening to " .. path .. "." .. member .. " -- every firing goes to @@SPY@@"
end

-- One property, followed: `@@WATCH game.Workspace.Part Transparency@@`. Its changes go to the same
-- log as a hooked remote, which is how a value that a script only ever sets from inside is read.
local function watch_property(arg)
	local path, member = (arg or ""):match("^(%S+)%s+(%S+)$")
	if not path then
		return "use @@WATCH path Property@@, e.g. @@WATCH game.Workspace.Part Transparency@@"
	end
	local node = resolve(path)
	if not node then return no_such(path) end
	local ok, problem = pcall(function()
		node:GetPropertyChangedSignal(member):Connect(function()
			local got, value = pcall(function() return tostring(node[member]) end)
			table.insert(SPY, "[CHANGED] " .. path .. "." .. member .. " = "
				.. (got and value or "?") )
		end)
	end)
	if not ok then
		return "could not watch " .. path .. "." .. member .. ": " .. tostring(problem)
	end
	return "watching " .. path .. "." .. member .. " -- every change goes to @@SPY@@"
end

local function fire_remote(arg)
	local path, rest = (arg or ""):match("^(%S+)%s*(.*)$")
	if not path then return "use @@FIRE path arg1 arg2@@ -- the arguments are Lua values" end
	local remote = resolve(path)
	if not remote then return no_such(path) end
	if not (remote:IsA("RemoteEvent") or remote:IsA("RemoteFunction")
		or remote:IsA("UnreliableRemoteEvent")) then
		return path .. " is a " .. remote.ClassName .. ", which is not a remote"
	end
	local args, shown = {}, {}
	for piece in (rest or ""):gmatch("[^,]+") do
		piece = piece:gsub("^%s+", ""):gsub("%s+$", "")
		if piece ~= "" then
			local value = value_of(piece)
			table.insert(args, value)
			table.insert(shown, tostring(value))
		end
	end
	table.insert(SPY, "[SENT] " .. path .. " (" .. table.concat(shown, " | ") .. ")")
	if remote:IsA("RemoteFunction") then
		local ok, result = pcall(function() return remote:InvokeServer(table.unpack(args)) end)
		if not ok then return "InvokeServer failed: " .. tostring(result) end
		return "InvokeServer(" .. table.concat(shown, ", ") .. ") returned: " .. tostring(result)
	end
	local ok, err = pcall(function() remote:FireServer(table.unpack(args)) end)
	if not ok then return "FireServer failed: " .. tostring(err) end
	return "fired " .. path .. " with " .. #args .. " argument(s): "
		.. (table.concat(shown, " | ") ~= "" and table.concat(shown, " | ") or "none")
end

-- Hook a function in place, so every call and every return value lands in @@SPY@@. This is how the
-- model reads a protocol it cannot see: hook the handler, trigger it, ask what it was handed.
local function hook_fn(path)
	if type(hookfunction) ~= "function" then return "this executor has no hookfunction" end
	local object, member = resolve_member(path)
	if not object then return no_such(path) end
	local original = member and object[member] or object
	if type(original) ~= "function" then return path .. " is not a function" end
	if HOOKED[path] then return "already logging " .. path end
	local replacement = function(...)
		local args = { ... }
		local parts = {}
		for i, value in ipairs(args) do parts[i] = tostring(value) end
		table.insert(SPY, "[CALLED] " .. path .. "(" .. table.concat(parts, " | ") .. ")")
		local ok, result = pcall(original, ...)
		table.insert(SPY, "[RETURN] " .. path .. " -> "
			.. (ok and tostring(result) or ("error: " .. tostring(result))))
		if not ok then error(result, 0) end
		return result
	end
	local ok, err = pcall(hookfunction, original, replacement)
	if not ok then return "hookfunction refused " .. path .. ": " .. tostring(err) end
	HOOKED[path] = true
	return "hooking " .. path .. " -- every call and return value goes to @@SPY@@"
end

-- The upvalues and the constants of a function: where a hidden handler keeps the remote it fires
-- and the strings it compares against.
local function function_parts(path, reader, label)
	if type(reader) ~= "function" then
		return "this executor has no " .. (label == "upvalues" and "getupvalues" or "getconstants")
	end
	local object, member = resolve_member(path)
	local fn = object and (member and object[member] or object)
	if type(fn) ~= "function" then return tostring(path) .. " is not a function" end
	local ok, list = pcall(reader, fn)
	if not ok then return label .. " refused " .. path .. ": " .. tostring(list) end
	local out = {}
	for i, value in ipairs(list or {}) do table.insert(out, i .. ": " .. tostring(value)) end
	if #out == 0 then return "no " .. label .. " on " .. path end
	return table.concat(out, "\n")
end

-- The garbage collector: every function alive in this client, by name. A script that was hidden
-- from the tree still has its handler running, and this is where it is found.
local function gc_scan(word)
	if type(getgc) ~= "function" then return "this executor has no getgc" end
	local ok, list = pcall(getgc, true)
	if not ok then return "getgc refused: " .. tostring(list) end
	local needle = (word or ""):lower()
	local total, functions, found = 0, 0, {}
	for _, value in ipairs(list or {}) do
		total = total + 1
		if type(value) == "function" then
			functions = functions + 1
			local name = ""
			pcall(function() name = tostring(debug.info(value, "n") or "") end)
			if needle == "" or name:lower():find(needle, 1, true) then
				if #found < 80 then
					table.insert(found, (name ~= "" and name or "anonymous") .. "  " .. tostring(value))
				end
			end
		end
	end
	table.insert(found, 1, string.format("%d object(s) alive, %d of them functions", total, functions))
	if #found == 1 then
		return found[1] .. "\nno function name matches " .. (word ~= "" and word or "(anything)")
	end
	return table.concat(found, "\n")
end

-- What this executor can do: the environment it hands a script. Asked with a filter, it is "is
-- there a primitive for this" -- the answer that decides what a script may assume.
local function env_scan(word)
	local genv = (type(getgenv) == "function" and getgenv()) or _G
	local needle = (word or ""):lower()
	local out = {}
	for key, value in pairs(genv) do
		local kind = type(value)
		if kind == "function" or kind == "table" then
			local name = tostring(key)
			if needle == "" or name:lower():find(needle, 1, true) then
				table.insert(out, name .. " (" .. kind .. ")")
			end
		end
	end
	table.sort(out)
	if #out == 0 then return "nothing in this executor's environment matches " .. (word ~= "" and word or "") end
	local capped = {}
	for i = 1, math.min(#out, 250) do capped[i] = out[i] end
	if #out > 250 then table.insert(capped, "... and " .. (#out - 250) .. " more") end
	return table.concat(capped, "\n")
end

local function fetch_text(url)
	if url == "" then return "no url given" end
	if not HTTP then
		return "this executor has no request function -- use @@EXEC@@ with HttpService:GetAsync instead"
	end
	local ok, res = pcall(HTTP, {Url = url, Method = "GET"})
	if not ok or type(res) ~= "table" then return "the request failed: " .. tostring(res) end
	local body = tostring(res.Body or "")
	if #body > 20000 then body = body:sub(1, 20000) .. "\n--[cut]" end
	return "HTTP " .. tostring(res.StatusCode) .. "\n" .. body
end

local function set_property(arg)
	local path, prop, written = (arg or ""):match("^(%S+)%s+(%S+)%s*(.*)$")
	if not path then return "use @@SET path Property value@@ -- value is a Lua value" end
	local node = resolve(path)
	if not node then return no_such(path) end
	local value = value_of(written)
	local ok, err = pcall(function() node[prop] = value end)
	if not ok and type(sethiddenproperty) == "function" then
		ok, err = pcall(sethiddenproperty, node, prop, value)
	end
	if not ok then return "could not set " .. path .. "." .. prop .. ": " .. tostring(err) end
	return path .. "." .. prop .. " = " .. tostring(value)
end

local function players()
	local out = {}
	for _, plr in ipairs(P:GetPlayers()) do
		local character = plr.Character
		local humanoid = character and character:FindFirstChildOfClass("Humanoid")
		local root = character and character:FindFirstChild("HumanoidRootPart")
		table.insert(out, table.concat({
			plr.Name .. " (" .. plr.DisplayName .. ")",
			"health=" .. (humanoid and (math.floor(humanoid.Health) .. "/" .. math.floor(humanoid.MaxHealth)) or "?"),
			"speed=" .. (humanoid and tostring(humanoid.WalkSpeed) or "?"),
			"pos=" .. (root and string.format("%.0f,%.0f,%.0f", root.Position.X, root.Position.Y, root.Position.Z) or "?"),
			"team=" .. (plr.Team and plr.Team.Name or "-"),
		}, "  "))
	end
	return table.concat(out, "\n")
end

local function executor_info()
	local name = "<unknown>"
	pcall(function()
		if identifyexecutor then name = tostring(identifyexecutor()) end
	end)
	-- What the deep tools need, and whether this executor hands it over: the answer decides which
	-- of them are worth asking for, and the ones about scripts decide whether a file this client
	-- was not sent the source of can be read at all.
	local can = {}
	for _, wanted in ipairs({"getscripts", "getinstances", "getloadedmodules", "getscriptbytecode",
		"decompile", "getgc", "getupvalues", "getconstants", "hookfunction", "getgenv"}) do
		if type(primitive(wanted)) == "function" then table.insert(can, wanted) end
	end
	local scripts = #all_scripts()
	return table.concat({
		"executor: " .. name,
		"loadstring: " .. (LOAD and "yes" or "no"),
		"http: " .. (HTTP and "yes" or "no"),
		"clipboard: " .. (clipboard and "yes" or "no"),
		"scripts in this client: " .. scripts,
		"primitives: " .. (#can > 0 and table.concat(can, ", ") or "none of the extra ones"),
		((type(primitive("decompile")) == "function")
			and "a decompiler is here, so @@SOURCE@@ reads a script whose source was never sent"
			or "no decompile(): scripts whose source was not sent cannot be read here"),
		"writer: " .. THINK .. " (the WRITER button switches it)",
		"place: " .. game.Name .. " (" .. tostring(game.PlaceId) .. ")",
		"player: " .. LP.Name .. " (" .. LP.DisplayName .. ")",
	}, "\n")
end

-- One script's source, rather than the whole game's: an agent that can read a single file is an
-- agent that can change one line in it instead of writing everything again.
local function source_at(path)
	local node = resolve(path)
	if not node then return no_such(path) end
	if not (node:IsA("Script") or node:IsA("LocalScript") or node:IsA("ModuleScript")) then
		return path_of(node) .. " is a " .. node.ClassName .. " and has no source to read"
	end
	-- The decompiler as well as `Source`: a LocalScript the client was never sent the text of is
	-- still a file in the game, and its bytecode is what there is of it.
	local src, how = script_source(node, 60000, true)
	if not src then
		return "could not read " .. path_of(node) .. ": " .. tostring(how)
	end
	return "[" .. node.ClassName .. "] " .. path_of(node) .. " (read from " .. how .. ")\n" .. src
end

-- The decompiler, asked for by name: the same text @@SOURCE@@ would give, but this one says how it
-- was got and how big the bytecode behind it is, which is what tells the model whether it is looking
-- at the game's own source or at a reconstruction of it.
local function decompile_at(path)
	local node = resolve(path)
	if not node then return no_such(path) end
	local ok, is_script = pcall(function()
		return node:IsA("Script") or node:IsA("LocalScript") or node:IsA("ModuleScript")
	end)
	if not ok or not is_script then
		return path_of(node) .. " is a " .. node.ClassName .. " and has no source to read"
	end
	local bytes = 0
	local reader = primitive("getscriptbytecode")
	if type(reader) == "function" then
		local got, raw = pcall(reader, node)
		if got and type(raw) == "string" then bytes = #raw end
	end
	local text, how = script_source(node, 60000, true)
	if not text then
		return "could not read " .. path_of(node) .. ": " .. tostring(how)
	end
	return table.concat({
		"[" .. node.ClassName .. "] " .. path_of(node),
		"read from: " .. how .. "  (bytecode: " .. bytes .. " bytes)",
		(text .. "\n"):gsub("\n$", ""),
	}, "\n")
end

-- What every hooked remote has fired with since it was hooked, newest last. The log the HOOK tool
-- fills in and, until now, nothing could read back.
local function spy_log()
	if #SPY == 0 then
		return "nothing is logged yet: use @@HOOK path@@ on a remote first, then ask again"
	end
	local out = {}
	for i = math.max(1, #SPY - 80), #SPY do table.insert(out, SPY[i]) end
	return table.concat(out, "\n")
end

-- A snippet run here and now, with its return value as well as its prints: the cheapest way for the
-- model to ask the game a question the dump cannot answer.
local function exec_snippet(code)
	if not code or code == "" then return "no code was given" end
	if not LOAD then return "there is no loadstring in this executor, so nothing can run" end
	local fn, err = LOAD(code)
	if not fn then return "compile error: " .. tostring(err) end
	local mark = #LOGS
	local ok, result = xpcall(fn, function(e)
		return tostring(e) .. "\n" .. tostring(debug.traceback("", 2))
	end)
	local printed = console_since(mark, 4000)
	return table.concat({
		ok and "ran without error" or "it failed",
		"returned: " .. tostring(result),
		printed ~= "" and ("printed:\n" .. printed) or "printed: (nothing)",
	}, "\n")
end

-- run_last is the RUN tool, and it needs the last script, which lives further down -- so it is
-- declared here and filled in below, rather than being a global by accident.
local run_last

local TOOLS = {
	{name = "DEEPSCAN", hint = "the whole game: remotes, scripts, module sources, values",
		run = function() return deep_scan() end},
	{name = "REMOTES", hint = "every RemoteEvent / RemoteFunction and its path",
		run = function() return remotes() end},
	{name = "SCRIPTS", hint = "every script and local script in the game, with the source of the ones this client has",
		run = function() return sources("script", 8000) end},
	{name = "MODULES", hint = "every module script in the game, with the source of the ones this client has",
		run = function() return sources("module", 8000) end},
	{name = "GREP", hint = "@@GREP word@@ the lines of script source containing word",
		run = function(arg) return grep(arg) end},
	{name = "FIND", hint = "@@FIND name@@ every instance whose name contains name",
		run = function(arg) return find_by_name(arg) end},
	{name = "TREE", hint = "@@TREE path@@ a shallow tree of the game, or of one instance",
		run = function(arg) return tree(arg) end},
	{name = "PROPS", hint = "@@PROPS path@@ the interesting properties of one instance",
		run = function(arg) return props(arg) end},
	{name = "DUMP_STRINGS", hint = "@@DUMP_STRINGS word@@ every string literal in the game's scripts containing word",
		run = function(arg) return strings(arg) end},
	{name = "HOOK", hint = "@@HOOK path@@ log what a remote fires with",
		run = function(arg) return hook(arg) end},
	{name = "UNHOOK", hint = "@@UNHOOK path@@ stop logging that remote",
		run = function(arg) return unhook(arg) end},
	{name = "PLAYERS", hint = "the players, their health, speed, position and team",
		run = function() return players() end},
	{name = "CONSOLE", hint = "the last lines this client printed",
		run = function() return console_since(math.max(0, #LOGS - 60), 4000) end},
	{name = "RUN", hint = "run the last script you wrote here and get its prints or its error",
		run = function() return run_last() end},
	{name = "SOURCE", hint = "@@SOURCE path@@ read one script of the game: its source, or the decompiled bytecode when the client was never sent the text",
		run = function(arg) return source_at(arg) end},
	{name = "DECOMPILE", hint = "@@DECOMPILE path@@ the same text, but saying whether it is the game's own source or a decompilation",
		run = function(arg) return decompile_at(arg) end},
	{name = "SPY", hint = "what every @@HOOK@@ed remote has fired with since it was hooked",
		run = function() return spy_log() end},
	{name = "EXEC", hint = "@@EXEC code@@ run a snippet here: what it returned, and what it printed",
		run = function(arg) return exec_snippet(arg) end},
	{name = "SELF", hint = "this client: what it can do, and the tools available",
		run = function() return executor_info() end},
	{name = "SIGNAL", hint = "@@SIGNAL path EventName@@ log everything that instance fires with",
		run = function(arg) return watch_signal(arg) end},
	{name = "WATCH", hint = "@@WATCH path Property@@ log every change of one property",
		run = function(arg) return watch_property(arg) end},
	{name = "FIRE", hint = "@@FIRE path arg1, arg2@@ fire or invoke a remote and read the reply",
		run = function(arg) return fire_remote(arg) end},
	{name = "HOOKFN", hint = "@@HOOKFN path@@ log every call of a function and what it returned",
		run = function(arg) return hook_fn(arg) end},
	{name = "UPVALUES", hint = "@@UPVALUES path@@ the upvalues of a function: what it is holding",
		run = function(arg) return function_parts(arg, primitive("getupvalues"), "upvalues") end},
	{name = "CONSTANTS", hint = "@@CONSTANTS path@@ the constants of a function: the strings it uses",
		run = function(arg) return function_parts(arg, primitive("getconstants"), "constants") end},
	{name = "GETGC", hint = "@@GETGC word@@ every live function in the client whose name contains word",
		run = function(arg) return gc_scan(arg) end},
	{name = "ENV", hint = "@@ENV word@@ what this executor hands a script: its functions and tables",
		run = function(arg) return env_scan(arg) end},
	{name = "HTTP", hint = "@@HTTP url@@ fetch a URL from here and read the body",
		run = function(arg) return fetch_text(arg) end},
	{name = "SET", hint = "@@SET path Property value@@ write a property (value is a Lua value)",
		run = function(arg) return set_property(arg) end},
}

local function tool_brief()
	local out = {"TOOLS you can call by writing the token in your answer; the client runs it and sends you the result:"}
	for _, tool in ipairs(TOOLS) do
		table.insert(out, "  @@" .. tool.name .. "@@ " .. tool.hint)
	end
	table.insert(out, table.concat({
		"Put each call on a line of its own, either as @@GREP remote@@ or as @@GREP@@ remote.",
		"For an argument longer than one line (an @@EXEC@@ snippet), write the token alone on its",
		"line, then the argument, then @@ alone on a line to close it.",
		"Everything here reads the game this client is running in -- its scripts, its remotes, its",
		"values and what it fires -- and never anything on the player's machine.",
		"The tokens are not code: never put one inside the script you send back, and never describe",
		"a call in prose instead of writing it -- a token is run, a sentence about one is not.",
	}, " "))
	return table.concat(out, "\n")
end

run_last = function()
	if last_code == "" then return "you have not written a script yet" end
	local ok, out = run_script(last_code)
	return (ok and "the script ran without error" or "the script failed") .. "\n" .. out
end

-- Every tool call in an answer, in the order it was written.
--
-- Both shapes the models actually write are read: @@GREP word@@, with the argument between the
-- tokens, and @@SOURCE@@ game.ReplicatedStorage.X, with it after them. A call ends at its closing
-- token or at the end of its line, whichever comes first -- and when the token is alone on its
-- line, at the next @@ instead, which is how a multi-line argument is written.
--
-- This used to be one pattern, @@([A-Z_]+)%s*([^@]*)@@, and that pattern ate the *next* call's
-- opening token as its own closing one: a block of six calls ran three of them and half of the
-- model's requests vanished, which is what a turn that "answered" without building anything was.
local function tool_calls_in(answer)
	local calls, text, pos = {}, answer or "", 1
	while true do
		local start, finish, name = text:find("@@([A-Z_]+)", pos)
		if not start then break end
		local after = finish + 1
		-- Spaces and tabs only: a call never spans two lines unless its token says so.
		while true do
			local char = text:sub(after, after)
			if char == " " or char == "\t" then after = after + 1 else break end
		end
		local close = text:find("@@", after, true)
		local newline = text:find("\n", after, true)
		local closing_here = close ~= nil and (newline == nil or close < newline)
		local arg, next_pos
		if closing_here and close > after then
			-- @@GREP word@@ -- the argument sits between the two tokens.
			arg = text:sub(after, close - 1)
			next_pos = close + 2
		elseif closing_here then
			-- @@SOURCE@@ path -- the token closed itself, so what follows on the line is the first
			-- of the argument, and the lines under it may go on being the argument up to a line
			-- holding nothing but @@. That closer is only taken as this call's when no other token
			-- comes first -- otherwise a call with an argument of its own (@@FIRE@@ Remote, 1) would
			-- swallow the call written under it.
			local rest = newline and text:sub(after + 2, newline - 1) or text:sub(after + 2)
			local from = newline or (#text + 1)
			local closer = text:find("\n@@", from, true)
			while closer do
				local end_of_that_line = text:find("\n", closer + 3, true) or (#text + 1)
				if text:sub(closer + 3, end_of_that_line - 1):match("^[ \t\r]*$") then break end
				closer = text:find("\n@@", closer + 3, true)
			end
			local other = text:find("@@[A-Z_]", from + 1)
			if closer and (not other or other > closer) then
				arg = rest .. text:sub(from, closer - 1)
				next_pos = (text:find("\n", closer + 3, true) or #text) + 1
			else
				arg = rest
				next_pos = from
			end
		else
			-- No closing token on this line at all: the rest of the line is the argument.
			arg = newline and text:sub(after, newline - 1) or text:sub(after)
			next_pos = newline and (newline + 1) or (#text + 1)
		end
		table.insert(calls, {name = name,
			arg = (arg or ""):gsub("^[ \t\r\n]+", ""):gsub("[ \t\r\n]+$", "")})
		pos = next_pos
	end
	return calls
end

-- How many calls one answer may run: enough for a scan and a follow-up, few enough that a model
-- that writes twenty of them cannot spend the turn's whole minute inside this function.
local MAXTOOLS = 8

-- Every tool call in an answer, run, as one block of text for the next turn.
local function run_tools(answer)
	local out, ran = {}, 0
	local calls = tool_calls_in(answer)
	for _, call in ipairs(calls) do
		local name, arg = call.name, call.arg
		local tool = nil
		for _, candidate in ipairs(TOOLS) do
			if candidate.name == name then tool = candidate break end
		end
		if not tool then
			table.insert(out, "@@" .. name .. "@@: there is no such tool")
		elseif ran >= MAXTOOLS then
			table.insert(out, "@@" .. name .. "@@: not run -- a turn runs at most " .. MAXTOOLS
				.. " tools. Ask for it again in the next answer if it still matters.")
		else
			ran = ran + 1
			local ok, result = pcall(tool.run, arg)
			table.insert(out, "@@" .. name .. (arg ~= "" and (" " .. arg) or "") .. "@@:\n"
				.. (ok and tostring(result) or ("the tool failed: " .. tostring(result))))
			if UI.onTool then UI.onTool(name, arg) end
		end
	end
	return table.concat(out, "\n\n")
end

-- =====================================================================================
-- 6. one turn on the service
-- =====================================================================================

local MSGS = {}
local busy = false

local function session_id()
	local id = ""
	for _ = 1, 4 do id = id .. string.format("%04x", math.random(0, 65535)) end
	return id
end
local SESSION = session_id()

local function trim()
	-- The system turn in front is never dropped, and neither is the newest exchange.
	while #MSGS > HISTORY + 1 do table.remove(MSGS, 2) end
end

-- Start one turn and watch it to the end. Returns:
--   text (everything it said), code (the script in it, "" when there is none)
-- Raises an error string when the turn itself failed, so the caller can show it.
local function ask(question)
	table.insert(MSGS, {role = "user", content = question})
	trim()
	local ok, started = api_retry("POST", "/chat/stream",
		{messages = MSGS, session = SESSION, mode = MODE, thinking = THINK})
	if not ok then table.remove(MSGS) error(started, 0) end

	local id = tostring(started.job or "")
	if id == "" then table.remove(MSGS) error("the service did not return a job", 0) end

	UI.setStatus("running", tostring(started.model or "") .. "  ·  " .. tostring(started.mode or MODE)
		.. "  ·  " .. tostring(started.thinking or THINK) .. "  ·  session " .. SESSION:sub(1, 6))
	local text, plan, thoughts, tools_done = "", "", "", ""
	local last_note, waited = "", 0
	-- How much the turn has produced, and how long it has been since it produced more. A turn that
	-- has stopped -- the provider's stream dying mid-answer, a socket open and silent -- reads
	-- exactly like a turn that is thinking, and the difference is the whole complaint: this client
	-- once sat on a dead turn for 555 seconds with the status line still saying "thinking". The
	-- service bounds its own reads (CHAT_IDLE), so this is the second net -- the one for a service
	-- that is up, answering polls, and no longer moving.
	local progress, quiet = 0, 0

	while true do
		task.wait(POLL)
		waited = waited + POLL
		local okr, data = api("GET", "/chat/result/" .. id)
		if okr then
			local status = tostring(data.status or "")
			-- A finished job's fields are final, so they are taken whichever way they compare to
			-- what was streamed. A turn that reset its answer would otherwise leave this client
			-- holding the version that was thrown away: half a script that looks whole, which is
			-- the one thing here that must never happen.
			if type(data.text) == "string" and (status == "done" or #data.text > #text) then
				text = data.text
			end
			if type(data.plan) == "string" and #data.plan > #plan then plan = data.plan end
			if type(data.tool) == "string" then tools_done = data.tool end
			if type(data.thoughts) == "string" and #data.thoughts > #thoughts then
				thoughts = data.thoughts
			end
			last_note = tostring(data.note or data.phase or "")
			-- Anything at all counts as progress: the answer, the thinking, a tool result, or the
			-- service saying it moved on to another phase. Nothing new for STALL seconds is a turn
			-- that is not running any more, whatever its status still says.
			local produced = #text + #thoughts + #plan + #tools_done + #last_note
			quiet = (produced > progress) and 0 or (quiet + POLL)
			progress = math.max(progress, produced)
			-- The mention, and the only place thinking shows at all now: not the chain of thought
			-- itself, but the fact that it is working one out -- how long it has been at it, and
			-- how much it has thought so far -- because a turn that says nothing for a minute reads
			-- as a broken turn. What the service says it is doing goes in front of the clock.
			local doing = (#text > 0) and "writing" or "thinking"
			local note = string.format("%ds", math.floor(waited))
			if last_note ~= "" then note = last_note .. "  ·  " .. note end
			if #text == 0 and #thoughts > 0 then
				note = note .. "  ·  " .. #thoughts .. " chars thought"
			end
			-- Said while it happens rather than only when it is over: "nothing new for 60s" is the
			-- difference between a turn that is slow and one that is not there, and it is a number
			-- the reader would otherwise be counting in their head.
			if quiet >= 30 then
				note = note .. "  ·  nothing new for " .. math.floor(quiet) .. "s"
			end
			UI.setStatus(doing, note)
			if quiet >= STALL then
				table.remove(MSGS)
				error(string.format("the service sent nothing new for %ds, so this turn was dropped"
					.. " -- ask again. It last said: %s", math.floor(quiet),
					(last_note ~= "" and last_note or "nothing")), 0)
			end
			-- The script pane only ever holds a script. The streamed answer is prose about the script
			-- -- with tool calls in it -- before there is a script at all, and a pane labelled SCRIPT
			-- full of that is what made a turn with nothing in it read as finished.
			-- Through only_code, never extract: the pane is what "copy code" would copy, so a turn
			-- that has written prose so far leaves it empty rather than filling a pane labelled
			-- SCRIPT with a paragraph of the model's notes.
			local sofar = only_code(text)
			if sofar ~= "" then UI.setCode(sofar) end
			if status == "error" then
				table.remove(MSGS)
				error(tostring(data.error or "the turn failed"), 0)
			end
			if status == "done" then break end
		else
			local why = tostring(data)
			if why:find("unknown or expired") then
				table.remove(MSGS)
				error(why, 0)
			end
			UI.setStatus("reconnecting", why:sub(1, 120))
			if waited > 600 then
				table.remove(MSGS)
				error("the service stopped answering", 0)
			end
		end
	end

	local code = extract(text)
	-- What is kept as the assistant turn is the ANSWER, whole. What the model sent is what the
	-- next turn is answered from, so nothing is ever summarised or cut here.
	table.insert(MSGS, {role = "assistant", content = (code ~= "" and code or text)})
	trim()
	return text, code, plan, tools_done
end

local function auto_rounds()
	local round, previous = 0, last_code
	while auto and round < MAXROUNDS do
		round = round + 1
		UI.setStatus("running", "auto run " .. round .. " of " .. MAXROUNDS)
		local ok, output = run_script(last_code)
		UI.bubble(ok and "system" or "error",
			"auto run " .. round .. ": " .. (ok and "no error" or "failed") .. "\n" .. output)
		local prompt = "RESULT OF RUNNING YOUR SCRIPT (round " .. round .. "):\n\n" .. output
			.. "\n\nIf it needs another fix, reply with ONLY the complete updated Luau script."
			.. " If it is working as asked, reply with ONLY that same script, unchanged."
		local got, text, code = pcall(ask, prompt)
		if not got then UI.bubble("error", tostring(text)) return end
		last_answer = text
		if code == "" then
			UI.bubble("system", "no script came back, so the auto loop stops here")
			return
		end
		-- It settled: the run was clean and the script came back the same. Asking again would only
		-- burn a turn on the same answer.
		if ok and code == previous then
			last_code = code
			UI.bubble("system", "it ran clean and the script stopped changing: done after "
				.. round .. " round(s)")
			return
		end
		previous, last_code = code, code
		UI.bubble("answer", text, code)
	end
	UI.bubble("system", "auto stopped after " .. round .. " round(s)")
end

-- One thing the user asked for, from start to finish: ask, run the model's own tools, keep the
-- whole answer, and (when auto is on) run it and feed the console back until it settles.
local function process(question)
	if busy then return end
	if question == nil or question == "" then return end
	busy = true
	UI.setBusy(true)
	-- The pane is cleared for the new turn: what is in it is the last answer's script, and
	-- leaving it there while the next answer is written is how a stale one gets copied.
	UI.setCode("")
	UI.bubble("user", question)
	local ok, text, code, plan = pcall(ask, question)
	if not ok then
		UI.bubble("error", tostring(text))
		UI.setStatus("failed", tostring(text))
		busy = false
		UI.setBusy(false)
		return
	end
	last_answer, last_code = text, code
	-- The answer, and then agent mode's plan under it: the plan is what the script was written from
	-- and the one part of the model's own thinking worth a turn of the transcript. The chain of
	-- thought itself is not shown at all -- the status line under the answer says it is thinking,
	-- and that is all a reader needs from it.
	if code ~= "" then UI.bubble("answer", text, code) else UI.bubble("system", text) end
	if plan and plan ~= "" then UI.bubble("plan", plan) end

	-- The tools the model asked for, run here -- twice at most, so a model that keeps asking
	-- cannot loop this forever.
	local passes = 0
	while passes < 2 do
		local results = run_tools(last_answer)
		if results == "" then break end
		passes = passes + 1
		UI.bubble("tools", results)
		local follow = "TOOL RESULTS:\n\n" .. results
			.. "\n\nNow reply with ONLY the complete Luau script you are building."
			.. " It must be the whole script, not the part that changed."
		local got, text2, code2 = pcall(ask, follow)
		if not got then
			UI.bubble("error", tostring(text2))
			break
		end
		last_answer = text2
		if code2 ~= "" then
			last_code = code2
			UI.bubble("answer", text2, code2)
		else
			UI.bubble("system", text2)
		end
	end

	if auto and last_code ~= "" then auto_rounds() end
	busy = false
	UI.setBusy(false)
	-- A turn that spent itself asking for tools and never wrote a script has not answered, and the
	-- status line says so instead of "idle": the difference between nothing to do and nothing came
	-- back is the one thing the transcript alone cannot show.
	if last_code == "" then
		UI.setStatus("no script", "the last answer was not one -- ask again, or say: now write it")
	else
		UI.setStatus("ready", "idle")
	end
end

-- =====================================================================================
-- 7. an error in the console, turned into a turn of its own
-- =====================================================================================

-- One console error, one turn. The error is the question; the script that produced it is
-- already the newest assistant turn of the same chat, so it is not carried again -- the model
-- answers in the conversation that wrote it, which is how every other question here is
-- answered.
--
-- Four things keep this from becoming a loop, because a script that fails every frame prints
-- the same error sixty times a second and a turn takes a minute:
--   * the same text twice is the same answer, so it is not asked again;
--   * a turn already running is waited for rather than talked over -- and rather than dropped:
--     what that turn is doing is very likely the answer to this error;
--   * a second error within ERR_FIX_WAIT of the last one is the tail of the same failure;
--   * ERR_FIX_MAX of them in a row and it stops and says so. A clean run puts that count back
--     to nothing, and so does a question the player typed themselves.
error_fix = function(line)
	local problem = tostring(line or ""):gsub("%s+$", "")
	if problem == "" then return end
	if problem == last_error then return end
	if os.time() - last_error_at < ERR_FIX_WAIT then return end
	local waited = 0
	while busy and waited < 120 do
		task.wait(1)
		waited = waited + 1
	end
	if busy or last_code == "" then return end
	-- Read again what was read before the wait: the turn that ran while this one waited may
	-- have been the answer to this very error, or to the one before it.
	if problem == last_error then return end
	if os.time() - last_error_at < ERR_FIX_WAIT then return end
	if error_fix_rounds >= ERR_FIX_MAX then
		UI.bubble("system", "that is " .. ERR_FIX_MAX .. " console errors in a row, so this one"
			.. " is not being sent: press .errors. to stop watching, or say what to change")
		UI.setStatus("failed", ERR_FIX_MAX .. " console errors in a row; the next one is not sent")
		return
	end
	error_fix_rounds = error_fix_rounds + 1
	last_error, last_error_at = problem, os.time()
	if #problem > 2000 then problem = problem:sub(1, 2000) .. "\n... (cut here)" end
	UI.bubble("error", "console error -- sent to the model with the script it came from:\n"
		.. problem)
	process(table.concat({
		"THE LAST SCRIPT YOU WROTE RAISED THIS IN THE CONSOLE:",
		"",
		problem,
		"",
		"That is the whole error, exactly as this client's console printed it. Fix the script it",
		" came from. Reply with ONLY the complete corrected Luau script -- the whole script, not the",
		" part that changed, and no prose.",
	}, "\n"))
end

-- =====================================================================================
-- 8. what a turn runs on: the script it just wrote, and the client itself
-- =====================================================================================

local function copy_code(code)
	local script = only_code(code)
	if script == "" then
		UI.bubble("system", "there is no script in that answer to copy")
		return
	end
	if clipboard(script) then
		UI.bubble("system", "copied " .. #script .. " characters of script -- no fence lines, no talk")
	else
		UI.bubble("system", "this executor has no clipboard function: press .full script. and copy by hand")
	end
end

-- Everything that is not a script: the console, the thinking. Copied exactly as it reads, with
-- nothing taken out of it, because the point of those two windows is that they are what happened.
local function copy_plain(text)
	local body = tostring(text or "")
	if body == "" then
		UI.bubble("system", "there is nothing in that window to copy")
		return
	end
	if clipboard(body) then
		UI.bubble("system", "copied " .. #body .. " characters, exactly as shown")
	else
		UI.bubble("system", "this executor has no clipboard function -- select the text by hand")
	end
end

local function execute(code, quiet)
	local script = only_code(code)
	if script == "" then
		UI.bubble("system", "there is no script in that answer to run")
		return
	end
	UI.setStatus("running", "executing " .. #script .. " characters")
	local ok, output = run_script(script)
	if not quiet then UI.bubble(ok and "system" or "error", output) end
	UI.setStatus(ok and "ready" or "failed", ok and "it ran" or "it failed -- see the console")
	if ok then error_fix_rounds, last_error = 0, "" end
	return ok
end

-- =====================================================================================
-- 9. the panel
-- =====================================================================================

local C = {
	bg      = Color3.fromRGB(9, 9, 15),
	panel   = Color3.fromRGB(17, 16, 26),
	card    = Color3.fromRGB(26, 24, 38),
	card2   = Color3.fromRGB(33, 30, 48),
	accent  = Color3.fromRGB(139, 92, 246),
	accent2 = Color3.fromRGB(34, 211, 238),
	text    = Color3.fromRGB(245, 243, 250),
	dim     = Color3.fromRGB(150, 143, 178),
	ok      = Color3.fromRGB(52, 211, 153),
	bad     = Color3.fromRGB(248, 113, 113),
	line    = Color3.fromRGB(52, 46, 74),
}
local MONO = Enum.Font.Code
local SANS = Enum.Font.Gotham
local SANS_B = Enum.Font.GothamBold
local SANS_M = Enum.Font.GothamMedium

local function mk(class, props, parent)
	local object = Instance.new(class)
	for key, value in pairs(props) do object[key] = value end
	if parent then object.Parent = parent end
	return object
end

local function round(object, radius)
	return mk("UICorner", {CornerRadius = UDim.new(0, radius or 10)}, object)
end

local function outline(object, color, thickness, transparency)
	return mk("UIStroke", {
		Color = color or C.line, Thickness = thickness or 1, Transparency = transparency or 0.2,
	}, object)
end

local function gradient(object, from, to, rotation)
	return mk("UIGradient", {
		Color = ColorSequence.new(from, to), Rotation = rotation or 0,
	}, object)
end

local function pad(object, top, bottom, left, right)
	return mk("UIPadding", {
		PaddingTop = UDim.new(0, top or 0), PaddingBottom = UDim.new(0, bottom or top or 0),
		PaddingLeft = UDim.new(0, left or 8), PaddingRight = UDim.new(0, right or left or 8),
	}, object)
end

-- --- the screen, and moving things around on it ----------------------------------------------
--
-- The panel is the screen: not a 760-pixel window parked somewhere in the middle of it, but every
-- pixel it is allowed to use, so nothing has to be scaled down to fit a phone and no edge of it
-- ends up out of reach. The one part of the screen that is not ours is the strip Roblox keeps for
-- its own buttons along the top, which is where the header would otherwise be sitting.
--
-- `wide` is the shape this file was written as -- a rail beside a working column -- and `narrow` is
-- the same pieces stacked, for a screen with no room to put the two side by side. Every position
-- below comes from `L`, and every window that opens is draggable once it exists.

local camera = workspace.CurrentCamera or workspace:WaitForChild("Camera", 5)

local function viewport()
	local size = (camera and camera.ViewportSize) or Vector2.new(1280, 720)
	return Vector2.new(math.max(size.X, 240), math.max(size.Y, 240))
end

-- Roblox's own buttons -- the menu, the chat, the mic -- live in a strip along the top of the
-- screen, and a panel drawn under it puts its header behind them, which is the one row of it that
-- has to stay reachable. This is the screen minus that strip.
local function safe_rect()
	local view = viewport()
	local top_left, bottom_right = GUIS:GetGuiInset()
	return {
		x = math.floor(top_left.X), y = math.floor(top_left.Y),
		w = math.max(240, view.X - top_left.X - bottom_right.X),
		h = math.max(180, view.Y - top_left.Y - bottom_right.Y),
	}
end

-- A desk-sized screen does not want a 2560-pixel transcript, so the panel fills the screen it has
-- up to a width a line of code is still readable at, and sits in the middle of the rest.
local WIDTH_CAP = 1100

local function usable_screen()
	local screen = safe_rect()
	local w = math.min(screen.w, WIDTH_CAP)
	return screen, {x = screen.x + math.floor((screen.w - w) / 2), y = screen.y, w = w, h = screen.h}
end

local view = viewport()
-- The rail is 132 wide and the column beside it wants 300 more: under 620 there is nothing to put
-- side by side, so the rail becomes a strip above the column and scrolls sideways instead.
local WIDE = view.X >= 620
local screen, panel = usable_screen()

-- One axis of a UDim2 in pixels, against the container's own size.
local function stretch(axis, base)
	return axis.Scale * base + axis.Offset
end

-- The ask box is the thing every eye is on after the script, so it is a share of the screen too
-- rather than a flat 40 pixels -- a comfortable field on a desktop and a sliver on a phone. 48 is
-- the floor, below which the box is smaller than the finger typing in it, and 62 the ceiling, past
-- which it is taking the transcript's room for one line of text.
local INPUT_H = math.clamp(math.floor(panel.h * 0.15), 48, 62)
-- Stacked on a phone: everything above the transcript, and the ask box and footer under it.
local NARROW_TOP = 124
local NARROW_BELOW = INPUT_H + 48
-- The status line left the header, so the script box took that row: its height is a share of the
-- screen it was handed rather than a fixed number of pixels, because 118 is a comfortable box on a
-- 900-pixel desktop and most of a phone lying sideways.
local CODE_H
if WIDE then
	CODE_H = math.clamp(math.floor(panel.h * 0.32), 132, 280)
else
	-- Stacked, the script box and the transcript are the pair sharing what the fixed rows leave, so
	-- 45% of the room goes to the box and neither one can push the other down into the ask box. The
	-- 96-pixel floor is four lines of Lua: a script box shorter than the script is not worth having.
	CODE_H = math.clamp(math.floor((panel.h - NARROW_TOP - NARROW_BELOW) * 0.45), 96, 280)
end

local L
if WIDE then
	-- The rail down the left, the working column beside it, and the panel itself is the screen.
	L = {
		panel = UDim2.new(0, panel.w, 0, panel.h), panel_at = UDim2.new(0, panel.x, 0, panel.y),
		overlay = UDim2.new(0, panel.w, 0, panel.h),
		overlay_at = UDim2.new(0, panel.x, 0, panel.y),
		rail = UDim2.new(0, 132, 1, -110), rail_at = UDim2.new(0, 12, 0, 48),
		rail_scroll = Enum.ScrollingDirection.Y, rail_auto = Enum.AutomaticSize.Y,
		rail_fill = Enum.FillDirection.Vertical, rail_h = Enum.HorizontalAlignment.Center,
		rail_v = Enum.VerticalAlignment.Top,
		rail_button = UDim2.new(1, 0, 0, 30), rail_mode = UDim2.new(1, 0, 0, 28),
		rail_label = UDim2.new(1, 0, 0, 14), rail_pad = {12, 12, 8, 8},
		code_label = UDim2.new(0, 160, 0, 46),
		code_at = UDim2.new(0, 156, 0, 62), code_size = UDim2.new(1, -328, 0, CODE_H),
		feed_at = UDim2.new(0, 156, 0, CODE_H + 80),
		feed_size = UDim2.new(1, -328, 1, -(CODE_H + 106 + INPUT_H)),
		input_at = UDim2.new(0, 156, 1, -(INPUT_H + 16)),
		input_size = UDim2.new(1, -156, 0, INPUT_H),
		send_at = UDim2.new(1, -104, 1, -(INPUT_H + 16)), send_size = UDim2.new(0, 88, 0, INPUT_H),
		footer_at = UDim2.new(0, 156, 1, -14), footer_size = UDim2.new(1, -300, 0, 14),
	}
else
	-- A phone held upright: the rail is a strip under the header, and the transcript gets whatever
	-- height is left over once the fixed rows above it have taken theirs.
	L = {
		panel = UDim2.new(0, panel.w, 0, panel.h), panel_at = UDim2.new(0, panel.x, 0, panel.y),
		overlay = UDim2.new(0, panel.w, 0, panel.h),
		overlay_at = UDim2.new(0, panel.x, 0, panel.y),
		rail = UDim2.new(1, -24, 0, 48), rail_at = UDim2.new(0, 12, 0, 44),
		rail_scroll = Enum.ScrollingDirection.X, rail_auto = Enum.AutomaticSize.X,
		rail_fill = Enum.FillDirection.Horizontal, rail_h = Enum.HorizontalAlignment.Left,
		rail_v = Enum.VerticalAlignment.Center,
		rail_button = UDim2.new(0, 108, 0, 36), rail_mode = UDim2.new(0, 78, 0, 36),
		rail_label = UDim2.new(0, 0, 0, 0), rail_pad = {6, 6, 6, 6},
		code_label = UDim2.new(0, 14, 0, 98),
		code_at = UDim2.new(0, 12, 0, 114), code_size = UDim2.new(1, -24, 0, CODE_H),
		-- Everything above the transcript is a fixed height, so the transcript is what is left of the
		-- screen -- measured from the same three numbers the script box was, which is what keeps the
		-- two of them and the ask box from ever landing on top of each other.
		feed_at = UDim2.new(0, 12, 0, NARROW_TOP + CODE_H),
		feed_size = UDim2.new(1, -24, 0,
			math.max(0, panel.h - (NARROW_TOP + CODE_H + NARROW_BELOW))),
		input_at = UDim2.new(0, 12, 1, -(INPUT_H + 38)),
		input_size = UDim2.new(1, -116, 0, INPUT_H),
		send_at = UDim2.new(1, -96, 1, -(INPUT_H + 38)), send_size = UDim2.new(0, 84, 0, INPUT_H),
		footer_at = UDim2.new(0, 12, 1, -34), footer_size = UDim2.new(1, -24, 0, 14),
	}
end

-- --- dragging, with a finger or with a mouse --------------------------------------------------
--
-- A finger and a mouse are the same gesture with two names, so the drag remembers which one started
-- it: a touch drag ends when the finger lifts, not when a mouse that never moved says so. The
-- movement is read from UserInputService rather than from the handle's own `InputChanged`, because
-- a handle's copy of that event stops arriving the moment the pointer leaves it -- which is exactly
-- what dragging a window out from under the pointer does.

local DRAG_START = {
	[Enum.UserInputType.Touch] = true,
	[Enum.UserInputType.MouseButton1] = true,
}

local function scale_of(object)
	local scale = object:FindFirstChildOfClass("UIScale")
	return scale and scale.Scale or 1
end

-- How big the object is on screen: its own UDim2 against the viewport (every window here is a child
-- of the ScreenGui, so that is its parent) times the scale it is drawn at.
local function shown_size(object, view)
	local size, scale = object.Size, scale_of(object)
	return stretch(size.X, view.X) * scale, stretch(size.Y, view.Y) * scale
end

-- Where its top-left corner is. Dragging works in corners while Position is whatever the object was
-- anchored by, and the two are only the same thing when the anchor is the top-left one.
local function corner_of(object, view)
	local w, h = shown_size(object, view)
	local anchor, position = object.AnchorPoint, object.Position
	return stretch(position.X, view.X) - anchor.X * w,
		stretch(position.Y, view.Y) - anchor.Y * h
end

local function place_corner(object, left, top, view)
	local w, h = shown_size(object, view)
	local anchor = object.AnchorPoint
	object.Position = UDim2.new(0, left + anchor.X * w, 0, top + anchor.Y * h)
end

-- A window put back inside the screen it is allowed to use, both edges clamped; an object wider
-- than that screen keeps its top-left edge, because no position would show more of it than the edge
-- does. The bounds are the usable screen rather than the viewport: the topbar strip is not ours.
local function clamp_to_view(object, view, screen)
	screen = screen or safe_rect()
	local w, h = shown_size(object, view)
	local left, top = corner_of(object, view)
	place_corner(object, math.clamp(left, screen.x, math.max(screen.x, screen.x + screen.w - w)),
		math.clamp(top, screen.y, math.max(screen.y, screen.y + screen.h - h)), view)
end

local raised = 210

-- Drag `object` by `handle` (by itself when there is no separate bar). The state comes back so a
-- button that is also a drag handle can tell a tap from a drag.
local function draggable(object, handle)
	handle = handle or object
	-- A Frame that is not Active does not answer a finger, however well it answers a mouse: this one
	-- line is the difference between a panel that moves and a panel that does not.
	if handle:IsA("GuiObject") then handle.Active = true end

	local state = {started = nil, grab = nil, from = nil, moved = false}

	handle.InputBegan:Connect(function(input)
		if not DRAG_START[input.UserInputType] then return end
		local view = viewport()
		state.started = input.UserInputType
		state.moved = false
		state.grab = Vector2.new(input.Position.X, input.Position.Y)
		state.from = Vector2.new(corner_of(object, view))
		if object.Parent and object.Parent:IsA("ScreenGui") then
			-- What was grabbed comes to the front, so the thing being moved is the thing being
			-- looked at even when two windows overlap.
			raised = raised + 1
			object.ZIndex = raised
		end
	end)

	UIS.InputChanged:Connect(function(input)
		if not state.started then return end
		-- The pointer that moves is the one that went down: a touch moves as a touch, a mouse as
		-- MouseMovement, and neither drives the other's drag.
		local moving = input.UserInputType == state.started
			or (state.started == Enum.UserInputType.MouseButton1
				and input.UserInputType == Enum.UserInputType.MouseMovement)
		if not moving then return end
		local now = Vector2.new(input.Position.X, input.Position.Y)
		if (now - state.grab).Magnitude > 4 then state.moved = true end
		-- Clamped as it goes rather than when it is let go: a window dragged past the edge has to
		-- stop there, or it is dragged somewhere that cannot be seen and then has to be hunted for.
		local view, screen = viewport(), safe_rect()
		local w, h = shown_size(object, view)
		place_corner(object,
			math.clamp(state.from.X + (now.X - state.grab.X), screen.x,
				math.max(screen.x, screen.x + screen.w - w)),
			math.clamp(state.from.Y + (now.Y - state.grab.Y), screen.y,
				math.max(screen.y, screen.y + screen.h - h)), view)
	end)

	UIS.InputEnded:Connect(function(input)
		if input.UserInputType == state.started then state.started = nil end
	end)

	return state
end

for _, old in ipairs(PG:GetChildren()) do
	if old.Name == "Ghaith" or old.Name == "Kanha" then old:Destroy() end
end

local gui = mk("ScreenGui", {
	Name = "Ghaith", ResetOnSpawn = false, IgnoreGuiInset = true, DisplayOrder = 9999,
	ZIndexBehavior = Enum.ZIndexBehavior.Sibling, Parent = PG,
})

-- --- the orb that opens it ------------------------------------------------------------------

local orb = mk("TextButton", {
	Name = "GhaithOrb", Size = UDim2.new(0, 58, 0, 58), Position = UDim2.new(1, -74, 0.5, -29),
	BackgroundColor3 = C.accent, Text = "G", TextColor3 = C.text, Font = SANS_B, TextSize = 24,
	BorderSizePixel = 0, AutoButtonColor = false, Active = true, ZIndex = 100, Parent = gui,
})
round(orb, 29)
local orb_gradient = gradient(orb, C.accent, Color3.fromRGB(168, 85, 247), 0)
outline(orb, Color3.fromRGB(255, 255, 255), 1, 0.75)
-- The orb is a drag handle as well as a button: on a phone it is the one thing always on screen,
-- so it is the one thing that has to be able to get out of the way. Its state is kept, because a
-- drag that ends over it also fires `Activated` and would otherwise toggle the panel as well.
local orb_drag = draggable(orb)

-- --- the frame ------------------------------------------------------------------------------

local main = mk("Frame", {
	Name = "GhaithPanel", Size = L.panel, Position = L.panel_at,
	BackgroundColor3 = C.bg, BorderSizePixel = 0,
	Visible = false, ZIndex = 50, Parent = gui, ClipsDescendants = true,
})
round(main, 18)
outline(main, C.line, 1, 0.25)
gradient(mk("Frame", {
	Size = UDim2.new(1, 0, 1, 0), BackgroundColor3 = Color3.fromRGB(255, 255, 255),
	BackgroundTransparency = 0.965, BorderSizePixel = 0, ZIndex = 50, Parent = main,
}), Color3.fromRGB(139, 92, 246), Color3.fromRGB(34, 211, 238), 30)

local header = mk("Frame", {
	Size = UDim2.new(1, 0, 0, 40), BackgroundColor3 = C.panel, BorderSizePixel = 0,
	ZIndex = 51, Parent = main,
})
local header_line = mk("Frame", {
	Size = UDim2.new(1, 0, 0, 2), Position = UDim2.new(0, 0, 1, -2), BorderSizePixel = 0,
	ZIndex = 52, Parent = header,
})
gradient(header_line, C.accent, C.accent2, 0)

mk("TextLabel", {
	Size = UDim2.new(0, 200, 0, 22), Position = UDim2.new(0, 18, 0, 9), BackgroundTransparency = 1,
	Text = "◈  GHAITH", TextColor3 = C.text, Font = SANS_B, TextSize = 18,
	TextXAlignment = Enum.TextXAlignment.Left, ZIndex = 53, Parent = header,
})
-- One row of header, and nothing else in it but the title and the close button. What the turn is
-- doing used to be said here, in a row of its own that cost the panel that row's height; it is said
-- at the foot of the transcript now (the status row below), under the answer it belongs to and in
-- the same run of the page as copy code and run, and the script box has the row instead.

local close_button = mk("TextButton", {
	Size = UDim2.new(0, 34, 0, 34), Position = UDim2.new(1, -46, 0.5, -17), BackgroundColor3 = C.card2,
	Text = "✕", TextColor3 = C.text, Font = SANS_B, TextSize = 15, BorderSizePixel = 0,
	AutoButtonColor = true, ZIndex = 53, Parent = header,
})
round(close_button, 10)

-- The panel moves by its header, which is the one strip of it that is never scrolled or typed in.
draggable(main, header)

-- --- the left rail --------------------------------------------------------------------------

local rail = mk("ScrollingFrame", {
	Size = L.rail, Position = L.rail_at,
	BackgroundColor3 = C.panel, BorderSizePixel = 0, ZIndex = 51, Parent = main,
	-- It scrolls rather than clips. Modes, actions and the thinking button are more than the rail
	-- is tall, and a button nobody can reach is worse than no button at all.
	ScrollBarThickness = 2, ScrollBarImageColor3 = C.line, CanvasSize = UDim2.new(0, 0, 0, 0),
	AutomaticCanvasSize = L.rail_auto, ScrollingDirection = L.rail_scroll,
	ClipsDescendants = true,
})
round(rail, 12)
outline(rail, C.line, 1, 0.4)
local rail_list = mk("UIListLayout", {
	Padding = UDim.new(0, 7), SortOrder = Enum.SortOrder.LayoutOrder,
	FillDirection = L.rail_fill, HorizontalAlignment = L.rail_h, VerticalAlignment = L.rail_v,
	Parent = rail,
})
pad(rail, L.rail_pad[1], L.rail_pad[2], L.rail_pad[3], L.rail_pad[4])

local MODES = {
	{id = "agent", label = "agent"},
	{id = "qwen", label = "qwen"},
	{id = "deepseek", label = "deepseek"},
}
local mode_buttons = {}

local function refresh_modes()
	for id, button in pairs(mode_buttons) do
		local on = id == MODE
		button.BackgroundColor3 = on and C.accent or C.card
		button.TextColor3 = on and C.text or C.dim
	end
end

local order = 0

-- Every button on the rail is made here: same shape, same hover, different job.
local function rail_button(text, color, callback)
	order = order + 1
	local base = color or C.card
	local button = mk("TextButton", {
		Size = L.rail_button, BackgroundColor3 = base, Text = text, TextColor3 = C.text,
		Font = SANS_B, TextSize = 12, BorderSizePixel = 0, AutoButtonColor = false,
		LayoutOrder = order, ZIndex = 52, Parent = rail,
	})
	round(button, 9)
	outline(button, C.line, 1, 0.5)
	button.MouseEnter:Connect(function()
		TW:Create(button, TweenInfo.new(0.14), {BackgroundColor3 = C.card2}):Play()
	end)
	button.MouseLeave:Connect(function()
		TW:Create(button, TweenInfo.new(0.14), {BackgroundColor3 = base}):Play()
	end)
	button.Activated:Connect(callback)
	return button
end

mk("TextLabel", {
	Size = L.rail_label, BackgroundTransparency = 1, Text = "MODE", TextColor3 = C.dim,
	Font = SANS_B, TextSize = 10, LayoutOrder = (function() order = order + 1 return order end)(),
	ZIndex = 52, Parent = rail,
})
for _, entry in ipairs(MODES) do
	order = order + 1
	local button = mk("TextButton", {
		Size = L.rail_mode, BackgroundColor3 = C.card, Text = entry.label,
		TextColor3 = C.dim, Font = SANS_B, TextSize = 12, BorderSizePixel = 0,
		AutoButtonColor = false, LayoutOrder = order, ZIndex = 52, Parent = rail,
	})
	round(button, 9)
	button.Activated:Connect(function()
		MODE = entry.id
		refresh_modes()
		UI.setStatus("ready", "next turn is " .. entry.id)
	end)
	mode_buttons[entry.id] = button
end
refresh_modes()

-- How much the writer reasons before it writes. One button, two states: the service takes the
-- setting per turn, so the same conversation can be asked in thinking and then asked again fast.
mk("TextLabel", {
	Size = L.rail_label, BackgroundTransparency = 1, Text = "WRITER", TextColor3 = C.dim,
	Font = SANS_B, TextSize = 10, LayoutOrder = (function() order = order + 1 return order end)(),
	ZIndex = 52, Parent = rail,
})
local think_button = mk("TextButton", {
	Size = L.rail_mode, BackgroundColor3 = C.card2, Text = "qwen: " .. THINK,
	TextColor3 = C.text, Font = SANS_B, TextSize = 12, BorderSizePixel = 0,
	AutoButtonColor = false, LayoutOrder = (function() order = order + 1 return order end)(),
	ZIndex = 52, Parent = rail,
})
round(think_button, 9)
outline(think_button, C.line, 1, 0.5)
think_button.Activated:Connect(function()
	THINK = (THINK == "thinking") and "fast" or "thinking"
	think_button.Text = "qwen: " .. THINK
	think_button.TextColor3 = (THINK == "fast") and C.accent2 or C.text
	UI.setStatus("ready", "the writer answers " .. THINK .. " from the next turn on")
end)

mk("TextLabel", {
	Size = L.rail_label, BackgroundTransparency = 1, Text = "ACTIONS", TextColor3 = C.dim,
	Font = SANS_B, TextSize = 10, LayoutOrder = (function() order = order + 1 return order end)(),
	ZIndex = 52, Parent = rail,
})

-- --- the right column -----------------------------------------------------------------------

-- The script box: one row lower down the panel than it used to be, and that much taller. The row
-- the header gave up when its status line moved into the transcript went here, and on top of that
-- the box is a share of the screen's height -- reading the whole script is what the panel is for,
-- and the transcript under it can scroll. What the turn is doing is said down there with it (the
-- status row below); the chain of thought itself is still never printed, because it is long, and
-- it is not what anybody is waiting for.

local code_label = mk("TextLabel", {
	Size = UDim2.new(0, 220, 0, 14), Position = L.code_label, BackgroundTransparency = 1,
	Text = "SCRIPT", TextColor3 = C.dim, Font = SANS_B, TextSize = 10,
	TextXAlignment = Enum.TextXAlignment.Left, ZIndex = 52, Parent = main,
})

local code_box = mk("TextBox", {
	Size = L.code_size, Position = L.code_at, BackgroundColor3 = C.panel,
	BorderSizePixel = 0, Text = "", PlaceholderText = "the script, as it is written",
	PlaceholderColor3 = C.dim, TextColor3 = C.text, Font = MONO, TextSize = 12, TextWrapped = true,
	TextXAlignment = Enum.TextXAlignment.Left, TextYAlignment = Enum.TextYAlignment.Top,
	ClearTextOnFocus = false, TextEditable = false, MultiLine = true, ZIndex = 51, Parent = main,
})
round(code_box, 10)
outline(code_box, C.line, 1, 0.45)
pad(code_box, 8, 8, 10, 10)

local feed = mk("ScrollingFrame", {
	Size = L.feed_size, Position = L.feed_at,
	BackgroundTransparency = 1, BorderSizePixel = 0, ScrollBarThickness = 4,
	ScrollBarImageColor3 = C.accent, CanvasSize = UDim2.new(0, 0, 0, 0),
	AutomaticCanvasSize = Enum.AutomaticSize.Y, ScrollingDirection = Enum.ScrollingDirection.Y,
	ClipsDescendants = true, ZIndex = 51, Parent = main,
})
mk("UIListLayout", {
	Padding = UDim.new(0, 8), SortOrder = Enum.SortOrder.LayoutOrder, Parent = feed,
})

-- What the turn is doing, said in the transcript, at the foot of it: "thinking · 12s · 340 chars
-- thought" while it works something out, "writing · 620 chars" once the script is coming, "ready ·
-- idle" when nothing is running, "no script" when a turn came back with none. Its LayoutOrder is
-- past any bubble's, so it stays the last row of the page and always reads directly under the
-- newest answer and the copy code / run buttons under it. The chain of thought itself is still
-- never printed -- only the fact that there is one, and how far along it is.
local status_row = mk("Frame", {
	Size = UDim2.new(0.96, 0, 0, 0), AutomaticSize = Enum.AutomaticSize.Y,
	BackgroundColor3 = C.card, BorderSizePixel = 0, LayoutOrder = 1000000, ZIndex = 52, Parent = feed,
})
round(status_row, 9)
outline(status_row, C.line, 1, 0.45)

local status_dot = mk("Frame", {
	Size = UDim2.new(0, 7, 0, 7), Position = UDim2.new(0, 11, 0, 12),
	BackgroundColor3 = C.ok, BorderSizePixel = 0, ZIndex = 53, Parent = status_row,
})
round(status_dot, 3)

local status_label = mk("TextLabel", {
	Size = UDim2.new(1, -36, 0, 0), Position = UDim2.new(0, 24, 0, 0),
	AutomaticSize = Enum.AutomaticSize.Y, BackgroundTransparency = 1,
	Text = "ready", TextColor3 = C.dim, Font = SANS_M, TextSize = 12, TextWrapped = true,
	TextXAlignment = Enum.TextXAlignment.Left, ZIndex = 53, Parent = status_row,
})
pad(status_label, 8, 8, 0, 0)

local input = mk("TextBox", {
	Size = L.input_size, Position = L.input_at, BackgroundColor3 = C.card,
	BorderSizePixel = 0, Text = "", PlaceholderText = "ask for a script…  (Enter sends)",
	PlaceholderColor3 = C.dim, TextColor3 = C.text, Font = SANS, TextSize = 16,
	TextXAlignment = Enum.TextXAlignment.Left, ClearTextOnFocus = false, ZIndex = 53, Parent = main,
})
round(input, 10)
outline(input, C.line, 1, 0.45)
pad(input, 0, 0, 12, 12)

local send_button = mk("TextButton", {
	Size = L.send_size, Position = L.send_at, BackgroundColor3 = C.accent,
	Text = "SEND", TextColor3 = C.text, Font = SANS_B, TextSize = 15, BorderSizePixel = 0,
	AutoButtonColor = false, ZIndex = 53, Parent = main,
})
round(send_button, 10)
gradient(send_button, C.accent, Color3.fromRGB(99, 102, 241), 0)

mk("TextLabel", {
	Size = L.footer_size, Position = L.footer_at, BackgroundTransparency = 1,
	Text = "Ghaith 2.0  ·  bahs agent service  ·  whole script every turn", TextColor3 = C.dim,
	Font = SANS, TextSize = 10, TextXAlignment = Enum.TextXAlignment.Left, ZIndex = 52, Parent = main,
})

-- --- the two overlays -----------------------------------------------------------------------

local function overlay(title, copy_kind)
	local frame = mk("Frame", {
		Size = L.overlay, Position = L.overlay_at,
		BackgroundColor3 = C.panel, BorderSizePixel = 0,
		Visible = false, ZIndex = 200, Parent = gui,
	})
	round(frame, 16)
	outline(frame, C.line, 1, 0.2)
	local bar = mk("Frame", {
		Size = UDim2.new(1, 0, 0, 46), BackgroundColor3 = C.card, BorderSizePixel = 0,
		ZIndex = 201, Parent = frame,
	})
	round(bar, 16)
	mk("TextLabel", {
		Size = UDim2.new(1, -180, 1, 0), Position = UDim2.new(0, 16, 0, 0), BackgroundTransparency = 1,
		Text = title, TextColor3 = C.text, Font = SANS_B, TextSize = 15,
		TextXAlignment = Enum.TextXAlignment.Left, ZIndex = 202, Parent = bar,
	})
	local body = mk("TextBox", {
		Size = UDim2.new(1, -24, 1, -62), Position = UDim2.new(0, 12, 0, 54),
		BackgroundColor3 = C.bg, BorderSizePixel = 0, Text = "", TextColor3 = C.text,
		Font = MONO, TextSize = 13, TextWrapped = true, TextXAlignment = Enum.TextXAlignment.Left,
		TextYAlignment = Enum.TextYAlignment.Top, ClearTextOnFocus = false, MultiLine = true,
		ZIndex = 201, Parent = frame,
	})
	round(body, 10)
	pad(body, 10, 10, 12, 12)
	if copy_kind then
		local copy = mk("TextButton", {
			Size = UDim2.new(0, 74, 0, 30), Position = UDim2.new(1, -162, 0.5, -15),
			BackgroundColor3 = C.ok, Text = "COPY", TextColor3 = C.text, Font = SANS_B,
			TextSize = 12, BorderSizePixel = 0, ZIndex = 202, Parent = bar,
		})
		round(copy, 9)
		copy.Activated:Connect(function()
			-- What COPY copies follows the window: a script window copies the script -- whole, and
			-- with no fence lines in it whatever the model wrapped it in -- while a console or
			-- thinking window copies exactly what it is showing, verbatim.
			if copy_kind == "code" then copy_code(body.Text) else copy_plain(body.Text) end
		end)
	end
	local shut = mk("TextButton", {
		Size = UDim2.new(0, 74, 0, 30), Position = UDim2.new(1, -80, 0.5, -15), BackgroundColor3 = C.bad,
		Text = "CLOSE", TextColor3 = C.text, Font = SANS_B, TextSize = 12, BorderSizePixel = 0,
		ZIndex = 202, Parent = bar,
	})
	round(shut, 9)
	shut.Activated:Connect(function() frame.Visible = false end)
	-- By its bar, with a finger or a mouse: a window that covers the panel it was opened from has
	-- to be movable on the screen it opened on.
	draggable(frame, bar)
	return frame, body
end

local code_window, code_window_body = overlay("THE SCRIPT  ·  whole, no fences", "code")
local console_window, console_window_body = overlay("CONSOLE  ·  everything this client printed", "text")
-- Exactly what scan game sent, byte for byte. The dump is the question the model is answering
-- -- every script with its text, then every other name in the game -- and a question that long
-- is worth being able to read: it is where "did it really send every script" is answered,
-- and where what the model was given can be counted without taking anybody's word for it.
local dump_window, dump_window_body = overlay("GAME DUMP  ·  the whole game, as it was sent", "text")

-- --- the transcript -------------------------------------------------------------------------

local bubble_order = 0
local function scroll_down()
	task.defer(function()
		feed.CanvasPosition = Vector2.new(0, feed.AbsoluteCanvasSize.Y)
	end)
end

UI.bubble = function(kind, text, code)
	bubble_order = bubble_order + 1
	local wrapper = mk("Frame", {
		Size = UDim2.new(1, -6, 0, 0), AutomaticSize = Enum.AutomaticSize.Y,
		BackgroundTransparency = 1, LayoutOrder = bubble_order, ZIndex = 52, Parent = feed,
	})
	local is_user = kind == "user"
	local body = mk("TextLabel", {
		Size = UDim2.new(is_user and 0.82 or 0.96, 0, 0, 0),
		Position = is_user and UDim2.new(1, 0, 0, 0) or UDim2.new(0, 0, 0, 0),
		AnchorPoint = is_user and Vector2.new(1, 0) or Vector2.new(0, 0),
		AutomaticSize = Enum.AutomaticSize.Y, BackgroundColor3 = C.card, BorderSizePixel = 0,
		Text = tostring(text or ""), TextColor3 = is_user and C.text or C.dim,
		Font = (kind == "answer" or kind == "tools") and MONO or SANS, TextSize = 13,
		TextWrapped = true, TextXAlignment = Enum.TextXAlignment.Left,
		TextYAlignment = Enum.TextYAlignment.Top, ZIndex = 52, Parent = wrapper,
	})
	round(body, 11)
	local padding = 9
	if kind == "error" then
		body.BackgroundColor3 = Color3.fromRGB(46, 24, 26)
		body.TextColor3 = C.bad
		outline(body, C.bad, 1, 0.5)
	elseif is_user then
		body.BackgroundColor3 = Color3.fromRGB(36, 30, 64)
		outline(body, C.accent, 1, 0.45)
	elseif kind == "answer" then
		body.BackgroundColor3 = C.panel
		body.TextColor3 = C.text
		outline(body, C.accent2, 1, 0.72)
	elseif kind == "tools" or kind == "plan" then
		-- Its own two turns, and neither is the reasoning: what the tools found, and (in agent
		-- mode) the plan the script was written from.
		body.BackgroundColor3 = C.panel
		body.TextColor3 = C.dim
		body.Text = (kind == "plan" and "◇ the plan it built from" or "◆ the tools it ran")
			.. "\n" .. tostring(text or "")
		outline(body, C.line, 1, 0.4)
	else
		body.BackgroundColor3 = C.card
	end
	pad(body, padding, padding, 12, 12)

	-- copy code, run and full are built only when the answer carries a script, and an answer
	-- that is a paragraph gets no bar at all: a button whose only possible answer is "there is no
	-- script in that answer" is worse than no button.
	if kind == "answer" and only_code(tostring(code or "")) ~= "" then
		local bar = mk("Frame", {
			Size = UDim2.new(0.96, 0, 0, 22), BackgroundTransparency = 1, ZIndex = 53, Parent = wrapper,
		})
		local function chip(text_label, color, callback, x, width)
			local button = mk("TextButton", {
				Size = UDim2.new(0, width, 0, 20), Position = UDim2.new(0, x, 0, 0),
				BackgroundColor3 = color, Text = text_label, TextColor3 = C.text, Font = SANS_B,
				TextSize = 11, BorderSizePixel = 0, AutoButtonColor = false, ZIndex = 54, Parent = bar,
			})
			round(button, 7)
			button.Activated:Connect(callback)
			return button
		end
		chip("copy code", C.accent, function() copy_code(code) end, 0, 74)
		chip("run", C.ok, function() execute(code) end, 80, 40)
		chip("full", C.card2, function()
			code_window_body.Text = only_code(code)
			code_window.Visible = true
		end, 126, 40)
		body.Position = UDim2.new(0, 0, 0, 26)
	end
	scroll_down()
	return body
end

UI.setCode = function(text)
	local body = text or ""
	code_box.Text = body
	code_box.TextColor3 = (#body > 0) and C.text or C.dim
	-- The label says how much is in the box, so "how long is this script" is a glance rather
	-- than a scroll to its last line: characters and lines while it holds one, plainly SCRIPT
	-- while it holds nothing.
	if #body == 0 then
		code_label.Text = "SCRIPT"
	else
		local lines = 1
		for _ in body:gmatch("\n") do lines = lines + 1 end
		code_label.Text = "SCRIPT  ·  " .. #body .. " chars  ·  " .. lines .. " lines"
	end
end

UI.setStatus = function(state, note)
	status_label.Text = tostring(state or "") .. (note and note ~= "" and ("  ·  " .. tostring(note)) or "")
	-- Dim words with a green dot while there is nothing to say, cyan while it is working, red when
	-- the turn came back with nothing: the dot is the glance, the words are the detail.
	local color, dot = C.dim, C.ok
	local lower = tostring(state):lower()
	if lower:find("fail") or lower:find("error") or lower:find("no script") then
		color, dot = C.bad, C.bad
	elseif lower == "running" or lower == "reconnecting" or lower == "retrying" then
		color, dot = C.accent2, C.accent2
	end
	status_label.TextColor3 = color
	status_dot.BackgroundColor3 = dot
	-- It is the last row of the transcript, so a reader who is already at the foot of it stays at the
	-- foot of it: a status that only ever updates below the fold is not a status. A reader who has
	-- scrolled up to read something is left where they are.
	if feed.AbsoluteCanvasSize.Y - feed.CanvasPosition.Y - feed.AbsoluteSize.Y < 80 then scroll_down() end
end

UI.setBusy = function(on)
	busy = on
	send_button.Text = on and "…" or "SEND"
	send_button.BackgroundColor3 = on and C.card2 or C.accent
end

local function submit()
	local question = input.Text
	if question == nil or question:gsub("%s", "") == "" then return end
	input.Text = ""
	error_fix_rounds, last_error = 0, ""
	process(question)
end

send_button.Activated:Connect(submit)
input.FocusLost:Connect(function(enter) if enter then submit() end end)

orb.Activated:Connect(function()
	if orb_drag.moved then
		orb_drag.moved = false   -- that was a drag, not a tap
		return
	end
	main.Visible = not main.Visible
	if main.Visible then
		-- Shown again where the screen can hold it, in case the screen changed behind it.
		clamp_to_view(main, viewport())
		TW:Create(main, TweenInfo.new(0.22, Enum.EasingStyle.Quint), {BackgroundTransparency = 0}):Play()
	end
end)
close_button.Activated:Connect(function() main.Visible = false end)

-- the rail buttons
order = order + 1
local scan_button = mk("TextButton", {
	Size = L.rail_button, BackgroundColor3 = C.card2, Text = "scan game",
	TextColor3 = C.text, Font = SANS_B, TextSize = 12, BorderSizePixel = 0, AutoButtonColor = false,
	LayoutOrder = order, ZIndex = 52, Parent = rail,
})
round(scan_button, 9)
scan_button.Activated:Connect(function()
	if busy then return end
	UI.setStatus("running", "reading the game")
	local dump, summary = deep_scan()
	-- Said before it is sent, not only once an answer arrives: the counts are what the scan
	-- found, and the window is the whole of what went out of here.
	dump_window_body.Text = dump
	dump_window.Visible = true
	UI.bubble("system", "scan game: " .. tostring(summary) .. " -- " .. #dump
		.. " characters of game sent as the question")
	process("GAME DUMP:\n\n" .. dump .. "\n\nRead the game above and reply with ONLY the"
		.. " complete Luau script for the most useful thing it makes possible.")
end)

-- run last, copy code and full script have left the rail: they are the buttons under the
-- answer that carries a script, which is the only place there is anything to copy or run.

order = order + 1
local console_button = mk("TextButton", {
	Size = L.rail_button, BackgroundColor3 = C.card, Text = "console",
	TextColor3 = C.text, Font = SANS_B, TextSize = 12, BorderSizePixel = 0, AutoButtonColor = false,
	LayoutOrder = order, ZIndex = 52, Parent = rail,
})
round(console_button, 9)
console_button.Activated:Connect(function()
	console_window_body.Text = console_since(0, 60000)
	console_window.Visible = true
end)

order = order + 1
local auto_button = mk("TextButton", {
	Size = L.rail_button, BackgroundColor3 = C.card, Text = "auto: off",
	TextColor3 = C.text, Font = SANS_B, TextSize = 12, BorderSizePixel = 0, AutoButtonColor = false,
	LayoutOrder = order, ZIndex = 52, Parent = rail,
})
round(auto_button, 9)
auto_button.Activated:Connect(function()
	auto = not auto
	auto_button.Text = auto and "auto: ON" or "auto: off"
	auto_button.BackgroundColor3 = auto and C.ok or C.card
	UI.setStatus("ready", auto and ("running and fixing itself, up to " .. MAXROUNDS .. " rounds") or "auto off")
end)

order = order + 1
order = order + 1
local errors_button = mk("TextButton", {
	Size = L.rail_button, BackgroundColor3 = C.ok, Text = "errors: ON",
	TextColor3 = C.text, Font = SANS_B, TextSize = 12, BorderSizePixel = 0, AutoButtonColor = false,
	LayoutOrder = order, ZIndex = 52, Parent = rail,
})
round(errors_button, 9)
errors_button.Activated:Connect(function()
	watch_errors = not watch_errors
	errors_button.Text = watch_errors and "errors: ON" or "errors: off"
	errors_button.BackgroundColor3 = watch_errors and C.ok or C.card
	UI.setStatus("ready", watch_errors
		and ("a console error is sent to the model for a fix, up to " .. ERR_FIX_MAX .. " in a row")
		or "console errors are not sent")
end)

-- The orb breathes while a turn is running, so a glance says whether it is working. It is a drag
-- handle as well as a button, so it pulses around wherever it has been put -- its position is read
-- back each tick rather than computed from the edge it started at, which would drag it home again.
task.spawn(function()
	local t = 0
	while orb.Parent do
		t = t + 0.045
		orb_gradient.Rotation = (math.sin(t) * 0.5 + 0.5) * 360
		local view = viewport()
		local left, top, w, h = corner_of(orb, view)
		local size = busy and (58 + math.sin(t * 3) * 3) or 58
		orb.Size = UDim2.new(0, size, 0, size)
		place_corner(orb, left + (w - size) / 2, top + (h - size) / 2, view)
		task.wait(0.03)
	end
end)

-- =====================================================================================
-- 10. boot
-- =====================================================================================

-- --- the screen it landed on ------------------------------------------------------------------
--
-- Every window inside the screen it is allowed to use, whatever it was placed at: the panel *is*
-- that screen now, the orb sits at its right edge, and none of that was written against the screen
-- this is running on. A phone that turns over is a different screen -- a shorter one, with the
-- topbar somewhere else -- so the fit runs again and the panel takes the new one whole.

local windows = {code_window, console_window, dump_window}

local function fit_to_screen()
	local view = viewport()
	local safe, rect = usable_screen()
	if not WIDE then
		-- Stacked, so the transcript is whatever height is left under the fixed rows above it.
		feed.Size = UDim2.new(1, -24, 0,
			math.max(0, rect.h - (NARROW_TOP + CODE_H + NARROW_BELOW)))
	end
	local size = UDim2.new(0, rect.w, 0, rect.h)
	local at = UDim2.new(0, rect.x, 0, rect.y)
	main.Size, main.Position = size, at
	for _, window in ipairs(windows) do
		window.Size, window.Position = size, at
	end
	clamp_to_view(main, view, safe)
	for _, window in ipairs(windows) do clamp_to_view(window, view, safe) end
	clamp_to_view(orb, view, safe)
end

fit_to_screen()
if camera then
	camera:GetPropertyChangedSignal("ViewportSize"):Connect(fit_to_screen)
end

MSGS = {{role = "system", content = SYSTEM .. "\n\n" .. tool_brief()}}
UI.setStatus("ready", "ask for a script, or press scan game")
UI.bubble("system", "ready. ask for a script, or press scan game. the status line at the foot of"
	.. " the transcript says thinking while it works something out -- the reasoning itself is not"
	.. " printed -- and the WRITER button switches the writer to fast, which is the same model"
	.. " without the reasoning. every answer comes back as one whole script, or the turn says it"
	.. " produced none instead of pretending. the tools marked @@ in the brief are run here and fed"
	.. " back to the model: it can read the game's scripts and modules, fire and hook remotes, and"
	.. " walk the garbage collector. drag a window's bar, or the orb, with your finger to move it.")

log("System", "Ghaith 2.0 loaded")
real_print("Ghaith 2.0 · " .. URL .. " · mode " .. MODE .. " · writer " .. THINK)
