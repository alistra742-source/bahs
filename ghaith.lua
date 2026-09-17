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
    one use: the header line reads "thinking · 12s · 340 chars thought" while the turn is working
    something out, so a quiet minute does not read as broken. The reasoning is not shown in a pane
    and not put in the transcript; it is long, and it is not what anybody is waiting for. "copy
    code" never copies it either: the script is the only thing here that is code.
  * The model can call tools on this client by writing @@NAME arg@@ in its answer. Those tokens
    are run here (game dump, remotes, sources, greps, hooks, players, the console, a live run)
    and the results go back as the next turn -- that is the agentic part.

Buttons: send (Enter), modes, scan game, run last, copy code (the script and nothing else), full
script, console, auto -- which runs what it wrote, hands the console back, gets a fix and repeats
until the script stops changing.

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
local POLL        = 0.8             -- seconds between reads of the running turn
local MAXROUNDS   = 4               -- auto: how many run-and-fix rounds it may take
local HISTORY     = 24              -- turns of conversation kept here (the service trims too)

local auto        = false           -- auto: run what it wrote, hand the console back, fix and repeat
local last_code   = ""              -- the script from the last answer, whole
local last_answer = ""              -- everything the last turn said

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

local function log(kind, text)
	table.insert(LOGS, {kind = kind, text = tostring(text), at = os.date("%H:%M:%S")})
	while #LOGS > 300 do table.remove(LOGS, 1) end
	if UI.onLog then UI.onLog(LOGS[#LOGS]) end
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

-- The script out of an answer. In order: a fenced block if one is there (a model that fenced
-- anyway); the whole answer when it reads as one script (the normal path -- this is what the
-- service sends); otherwise the longest run of lines that read as code. "" means the answer
-- carried no script, which is what the re-ask keys off.
local function extract(text)
	if not text or text == "" then return "" end
	local t = text:gsub("\r\n", "\n"):gsub("\r", "\n")

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
	return ""
end

-- What a copy button actually copies: the script, and never a fence line, even if a model wrote
-- one anyway.
local function only_code(text)
	local code = extract(text)
	if code == "" then code = text or "" end
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

local function resolve(path)
	local node = game
	if path and path ~= "" then
		for piece in path:gmatch("[^%.]+") do
			node = node:FindFirstChild(piece)
			if not node then return nil end
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

-- Every instance under the usual services, with the things worth reading written out.
local function deep_scan()
	local out, budget = {}, 180000
	local counts = {all = 0, remote = 0, script = 0, module = 0, value = 0}
	local function add(line)
		budget = budget - #line - 1
		if budget < 0 then return end
		table.insert(out, line)
	end
	add("GAME DUMP  " .. game.Name .. "  place " .. tostring(game.PlaceId))
	for _, name in ipairs(SERVICES) do
		local service = game:FindFirstChild(name)
		if service then
			add("\n== " .. name .. " ==")
			for _, d in ipairs(service:GetDescendants()) do
				counts.all = counts.all + 1
				local p = path_of(d)
				if d:IsA("RemoteEvent") or d:IsA("RemoteFunction") or d:IsA("UnreliableRemoteEvent") then
					counts.remote = counts.remote + 1
					add("[REMOTE " .. d.ClassName .. "] " .. p)
				elseif d:IsA("ModuleScript") then
					counts.module = counts.module + 1
					add("[MODULE] " .. p)
					local src = source_of(d, 4000)
					if src then add(src) end
				elseif d:IsA("Script") or d:IsA("LocalScript") then
					counts.script = counts.script + 1
					add("[" .. d.ClassName .. "] " .. p)
					local src = source_of(d, 4000)
					if src then add(src) end
				elseif d:IsA("ValueBase") then
					counts.value = counts.value + 1
					add("[VALUE " .. d.ClassName .. "] " .. p .. " = " .. tostring(d.Value))
				end
			end
		end
	end
	add(string.format("\n== %d instances, %d remotes, %d scripts, %d modules ==",
		counts.all, counts.remote, counts.script, counts.module))
	return table.concat(out, "\n")
end

local function remotes()
	local out = {}
	for _, name in ipairs(SERVICES) do
		local service = game:FindFirstChild(name)
		if service then
			for _, d in ipairs(service:GetDescendants()) do
				if d:IsA("RemoteEvent") or d:IsA("RemoteFunction") or d:IsA("UnreliableRemoteEvent") then
					table.insert(out, "[" .. d.ClassName .. "] " .. path_of(d))
				end
			end
		end
	end
	return table.concat(out, "\n")
end

local function sources(kind, cap)
	local out = {}
	for _, name in ipairs(SERVICES) do
		local service = game:FindFirstChild(name)
		if service then
			for _, d in ipairs(service:GetDescendants()) do
				if d:IsA("ModuleScript") and (kind == "module" or kind == "all") then
					table.insert(out, "[MODULE] " .. path_of(d))
					local src = source_of(d, cap)
					if src then table.insert(out, src) end
				elseif (d:IsA("Script") or d:IsA("LocalScript"))
					and (kind == "script" or kind == "all") then
					table.insert(out, "[" .. d.ClassName .. "] " .. path_of(d))
					local src = source_of(d, cap)
					if src then table.insert(out, src) end
				end
			end
		end
	end
	return table.concat(out, "\n\n")
end

local function grep(word)
	if not word or word == "" then return "no word given" end
	local out, needle = {}, word:lower()
	for _, name in ipairs(SERVICES) do
		local service = game:FindFirstChild(name)
		if service then
			for _, d in ipairs(service:GetDescendants()) do
				if d:IsA("Script") or d:IsA("LocalScript") or d:IsA("ModuleScript") then
					local src = source_of(d, 30000)
					if src and src:lower():find(needle, 1, true) then
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
			end
		end
	end
	if #out == 0 then return "nothing in the game's scripts contains " .. word end
	return table.concat(out, "\n")
end

local function find_by_name(name)
	if not name or name == "" then return "no name given" end
	local out, needle = {}, name:lower()
	for _, name_service in ipairs(SERVICES) do
		local service = game:FindFirstChild(name_service)
		if service then
			for _, d in ipairs(service:GetDescendants()) do
				if d.Name:lower():find(needle, 1, true) then
					table.insert(out, "[" .. d.ClassName .. "] " .. path_of(d))
				end
			end
		end
	end
	if #out == 0 then return "nothing is named like " .. name end
	return table.concat(out, "\n")
end

local function tree(root)
	local node = resolve(root)
	if not node then return "there is no " .. tostring(root) end
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
	if not node then return "there is no " .. tostring(path) end
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

local function strings()
	local out = {}
	for _, name in ipairs(SERVICES) do
		local service = game:FindFirstChild(name)
		if service then
			for _, d in ipairs(service:GetDescendants()) do
				if d:IsA("Script") or d:IsA("LocalScript") or d:IsA("ModuleScript") then
					local src = source_of(d, 30000)
					if src then
						for literal in src:gmatch('"(.-)"') do
							if #literal >= 4 and #literal <= 120 then
								table.insert(out, path_of(d) .. " -> " .. literal)
							end
						end
					end
				end
			end
		end
	end
	return table.concat(out, "\n")
end

local HOOKED = {}
local SPY = {}

local function hook(path)
	local remote = resolve(path)
	if not remote then return "there is no " .. tostring(path) end
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
	if not remote then return "there is no " .. tostring(path) end
	HOOKED[remote] = nil
	return "stopped logging " .. path
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
	return table.concat({
		"executor: " .. name,
		"loadstring: " .. (LOAD and "yes" or "no"),
		"http: " .. (HTTP and "yes" or "no"),
		"clipboard: " .. (clipboard and "yes" or "no"),
		"place: " .. game.Name .. " (" .. tostring(game.PlaceId) .. ")",
		"player: " .. LP.Name .. " (" .. LP.DisplayName .. ")",
	}, "\n")
end

-- One script's source, rather than the whole game's: an agent that can read a single file is an
-- agent that can change one line in it instead of writing everything again.
local function source_at(path)
	local node = resolve(path)
	if not node then return "there is no " .. tostring(path) end
	if not (node:IsA("Script") or node:IsA("LocalScript") or node:IsA("ModuleScript")) then
		return path_of(node) .. " is a " .. node.ClassName .. " and has no source to read"
	end
	local src = source_of(node, 40000)
	if not src then
		return "the source of " .. path_of(node) .. " reads as empty (no ProtectedString source)"
	end
	return "[" .. node.ClassName .. "] " .. path_of(node) .. "\n" .. src
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
	{name = "SCRIPTS", hint = "every script and local script with its source",
		run = function() return sources("script", 4000) end},
	{name = "MODULES", hint = "every module script with its source",
		run = function() return sources("module", 6000) end},
	{name = "GREP", hint = "@@GREP word@@ the lines of script source containing word",
		run = function(arg) return grep(arg) end},
	{name = "FIND", hint = "@@FIND name@@ every instance whose name contains name",
		run = function(arg) return find_by_name(arg) end},
	{name = "TREE", hint = "@@TREE path@@ a shallow tree of the game, or of one instance",
		run = function(arg) return tree(arg) end},
	{name = "PROPS", hint = "@@PROPS path@@ the interesting properties of one instance",
		run = function(arg) return props(arg) end},
	{name = "DUMP_STRINGS", hint = "every string literal in the game's scripts",
		run = function() return strings() end},
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
	{name = "SOURCE", hint = "@@SOURCE path@@ the source of one script or module, on its own",
		run = function(arg) return source_at(arg) end},
	{name = "SPY", hint = "what every @@HOOK@@ed remote has fired with since it was hooked",
		run = function() return spy_log() end},
	{name = "EXEC", hint = "@@EXEC code@@ run a snippet here: what it returned, and what it printed",
		run = function(arg) return exec_snippet(arg) end},
	{name = "SELF", hint = "this client: what it can do, and the tools available",
		run = function() return executor_info() end},
}

local function tool_brief()
	local out = {"TOOLS you can call by writing the token in your answer; the client runs it and sends you the result:"}
	for _, tool in ipairs(TOOLS) do
		table.insert(out, "  @@" .. tool.name .. "@@ " .. tool.hint)
	end
	table.insert(out, "Example: write @@GREP remote@@ on a line of its own. The token is not code: never put one inside the script.")
	return table.concat(out, "\n")
end

run_last = function()
	if last_code == "" then return "you have not written a script yet" end
	local ok, out = run_script(last_code)
	return (ok and "the script ran without error" or "the script failed") .. "\n" .. out
end

-- Every @@NAME arg@@ in an answer, run, as one block of text for the next turn.
local function run_tools(answer)
	local out = {}
	for name, arg in (answer or ""):gmatch("@@([A-Z_]+)%s*([^@]*)@@") do
		local tool = nil
		for _, candidate in ipairs(TOOLS) do
			if candidate.name == name then tool = candidate break end
		end
		arg = arg:gsub("^%s+", ""):gsub("%s+$", "")
		if not tool then
			table.insert(out, "@@" .. name .. "@@: there is no such tool")
		else
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
		{messages = MSGS, session = SESSION, mode = MODE})
	if not ok then table.remove(MSGS) error(started, 0) end

	local id = tostring(started.job or "")
	if id == "" then table.remove(MSGS) error("the service did not return a job", 0) end

	UI.setStatus("running", tostring(started.model or "") .. "  ·  " .. tostring(started.mode or MODE)
		.. "  ·  session " .. SESSION:sub(1, 6))
	local text, plan, thoughts, tools_done = "", "", "", ""
	local last_note, waited = "", 0

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
			UI.setStatus(doing, note)
			UI.setCode(text)
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
	-- thought itself is not shown at all -- the header says it is thinking, and that is all a reader
	-- needs from it.
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
	UI.setStatus("ready", "idle")
end

-- =====================================================================================
-- 7. what a turn runs on: the script it just wrote, and the client itself
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
	return ok
end

-- =====================================================================================
-- 8. the panel
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

local L
if WIDE then
	-- The rail down the left, the working column beside it, and the panel itself is the screen.
	L = {
		panel = UDim2.new(0, panel.w, 0, panel.h), panel_at = UDim2.new(0, panel.x, 0, panel.y),
		overlay = UDim2.new(0, panel.w, 0, panel.h),
		overlay_at = UDim2.new(0, panel.x, 0, panel.y),
		rail = UDim2.new(0, 132, 1, -124), rail_at = UDim2.new(0, 12, 0, 62),
		rail_scroll = Enum.ScrollingDirection.Y, rail_auto = Enum.AutomaticSize.Y,
		rail_fill = Enum.FillDirection.Vertical, rail_h = Enum.HorizontalAlignment.Center,
		rail_v = Enum.VerticalAlignment.Top,
		rail_button = UDim2.new(1, 0, 0, 30), rail_mode = UDim2.new(1, 0, 0, 28),
		rail_label = UDim2.new(1, 0, 0, 14), rail_pad = {12, 12, 8, 8},
		code_label = UDim2.new(0, 160, 0, 60),
		code_at = UDim2.new(0, 156, 0, 76), code_size = UDim2.new(1, -328, 0, 118),
		feed_at = UDim2.new(0, 156, 0, 212), feed_size = UDim2.new(1, -328, 1, -274),
		input_at = UDim2.new(0, 156, 1, -52), input_size = UDim2.new(1, -156, 0, 40),
		send_at = UDim2.new(1, -104, 1, -52), send_size = UDim2.new(0, 88, 0, 40),
		footer_at = UDim2.new(0, 156, 1, -14), footer_size = UDim2.new(1, -300, 0, 14),
	}
else
	-- A phone held upright: the rail is a strip under the header, and the transcript gets whatever
	-- height is left over once the fixed rows above it have taken theirs.
	L = {
		panel = UDim2.new(0, panel.w, 0, panel.h), panel_at = UDim2.new(0, panel.x, 0, panel.y),
		overlay = UDim2.new(0, panel.w, 0, panel.h),
		overlay_at = UDim2.new(0, panel.x, 0, panel.y),
		rail = UDim2.new(1, -24, 0, 48), rail_at = UDim2.new(0, 12, 0, 58),
		rail_scroll = Enum.ScrollingDirection.X, rail_auto = Enum.AutomaticSize.X,
		rail_fill = Enum.FillDirection.Horizontal, rail_h = Enum.HorizontalAlignment.Left,
		rail_v = Enum.VerticalAlignment.Center,
		rail_button = UDim2.new(0, 108, 0, 36), rail_mode = UDim2.new(0, 78, 0, 36),
		rail_label = UDim2.new(0, 0, 0, 0), rail_pad = {6, 6, 6, 6},
		code_label = UDim2.new(0, 14, 0, 112),
		code_at = UDim2.new(0, 12, 0, 128), code_size = UDim2.new(1, -24, 0, 100),
		-- Everything above the transcript is a fixed height, so the transcript is what is left of
		-- the screen: it keeps a floor of 120, so a very short screen scrolls a small transcript
		-- rather than an invisible one.
		feed_at = UDim2.new(0, 12, 0, 238),
		feed_size = UDim2.new(1, -24, 0, math.max(120, panel.h - 326)),
		input_at = UDim2.new(0, 12, 1, -78), input_size = UDim2.new(1, -116, 0, 40),
		send_at = UDim2.new(1, -96, 1, -78), send_size = UDim2.new(0, 84, 0, 40),
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
	Size = UDim2.new(1, 0, 0, 54), BackgroundColor3 = C.panel, BorderSizePixel = 0,
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
local status_label = mk("TextLabel", {
	Size = UDim2.new(1, -230, 0, 16), Position = UDim2.new(0, 18, 0, 31), BackgroundTransparency = 1,
	Text = "ready", TextColor3 = C.dim, Font = SANS_M, TextSize = 12,
	TextXAlignment = Enum.TextXAlignment.Left, TextTruncate = Enum.TextTruncate.AtEnd,
	ZIndex = 53, Parent = header,
})
local pulse_dot = mk("Frame", {
	Size = UDim2.new(0, 8, 0, 8), Position = UDim2.new(1, -104, 0.5, -4), BackgroundColor3 = C.ok,
	BorderSizePixel = 0, ZIndex = 53, Parent = header,
})
round(pulse_dot, 4)

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

mk("TextLabel", {
	Size = L.rail_label, BackgroundTransparency = 1, Text = "ACTIONS", TextColor3 = C.dim,
	Font = SANS_B, TextSize = 10, LayoutOrder = (function() order = order + 1 return order end)(),
	ZIndex = 52, Parent = rail,
})

-- --- the right column -----------------------------------------------------------------------

-- Thinking is *said*, never printed. A pane used to sit here and scroll the model's own chain of
-- thought, and it was the wrong thing to watch: it is long, it is not what the reader is waiting
-- for, and it pushed the script and the transcript into a corner of the panel. What is left of it
-- is the mention in the header -- "thinking · 12s · 340 chars thought" -- which says the turn is
-- alive and working something out without spending a pane, or a bubble, on the reasoning itself.

local code_label = mk("TextLabel", {
	Size = UDim2.new(0, 100, 0, 14), Position = L.code_label, BackgroundTransparency = 1,
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

local input = mk("TextBox", {
	Size = L.input_size, Position = L.input_at, BackgroundColor3 = C.card,
	BorderSizePixel = 0, Text = "", PlaceholderText = "ask for a script…  (Enter sends)",
	PlaceholderColor3 = C.dim, TextColor3 = C.text, Font = SANS, TextSize = 14,
	TextXAlignment = Enum.TextXAlignment.Left, ClearTextOnFocus = false, ZIndex = 53, Parent = main,
})
round(input, 10)
outline(input, C.line, 1, 0.45)
pad(input, 0, 0, 12, 12)

local send_button = mk("TextButton", {
	Size = L.send_size, Position = L.send_at, BackgroundColor3 = C.accent,
	Text = "SEND", TextColor3 = C.text, Font = SANS_B, TextSize = 13, BorderSizePixel = 0,
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
		Font = (kind == "answer" or kind == "tools") and MONO or SANS, TextSize = 12,
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

	if kind == "answer" then
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
	code_box.Text = text or ""
	code_box.TextColor3 = (#(text or "") > 0) and C.text or C.dim
end

UI.setStatus = function(state, note)
	status_label.Text = tostring(state or "") .. (note and note ~= "" and ("  ·  " .. tostring(note)) or "")
	local color = C.ok
	local lower = tostring(state):lower()
	if lower:find("fail") or lower:find("error") then color = C.bad end
	if lower == "running" or lower == "reconnecting" or lower == "retrying" then color = C.accent2 end
	pulse_dot.BackgroundColor3 = color
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
	local dump = deep_scan()
	process("GAME DUMP:\n\n" .. dump .. "\n\nRead the game above and reply with ONLY the"
		.. " complete Luau script for the most useful thing it makes possible.")
end)

order = order + 1
local run_button = mk("TextButton", {
	Size = L.rail_button, BackgroundColor3 = C.card, Text = "run last",
	TextColor3 = C.text, Font = SANS_B, TextSize = 12, BorderSizePixel = 0, AutoButtonColor = false,
	LayoutOrder = order, ZIndex = 52, Parent = rail,
})
round(run_button, 9)
run_button.Activated:Connect(function()
	if busy then return end
	if last_code == "" then
		UI.bubble("system", "no script yet")
		return
	end
	execute(last_code)
end)

order = order + 1
local copy_button = mk("TextButton", {
	Size = L.rail_button, BackgroundColor3 = C.card, Text = "copy code",
	TextColor3 = C.text, Font = SANS_B, TextSize = 12, BorderSizePixel = 0, AutoButtonColor = false,
	LayoutOrder = order, ZIndex = 52, Parent = rail,
})
round(copy_button, 9)
copy_button.Activated:Connect(function() copy_code(last_code ~= "" and last_code or last_answer) end)

order = order + 1
local full_button = mk("TextButton", {
	Size = L.rail_button, BackgroundColor3 = C.card, Text = "full script",
	TextColor3 = C.text, Font = SANS_B, TextSize = 12, BorderSizePixel = 0, AutoButtonColor = false,
	LayoutOrder = order, ZIndex = 52, Parent = rail,
})
round(full_button, 9)
full_button.Activated:Connect(function()
	code_window_body.Text = only_code(last_code ~= "" and last_code or last_answer)
	code_window.Visible = true
end)

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
-- 9. boot
-- =====================================================================================

-- --- the screen it landed on ------------------------------------------------------------------
--
-- Every window inside the screen it is allowed to use, whatever it was placed at: the panel *is*
-- that screen now, the orb sits at its right edge, and none of that was written against the screen
-- this is running on. A phone that turns over is a different screen -- a shorter one, with the
-- topbar somewhere else -- so the fit runs again and the panel takes the new one whole.

local windows = {code_window, console_window}

local function fit_to_screen()
	local view = viewport()
	local safe, rect = usable_screen()
	if not WIDE then
		-- Stacked, so the transcript is whatever height is left under the fixed rows above it.
		feed.Size = UDim2.new(1, -24, 0, math.max(120, rect.h - 326))
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
UI.bubble("system", "ready. ask for a script, or press scan game. the header says thinking while it"
	.. " is working something out -- the reasoning itself is not printed -- and every answer comes"
	.. " back as one whole script. drag a window's bar, or the orb, with your finger to move it.")

log("System", "Ghaith 2.0 loaded")
real_print("Ghaith 2.0 · " .. URL .. " · mode " .. MODE)
