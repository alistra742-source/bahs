-- bahs: the in-game side of the bridge -- ask, and (if you leave "listen" on) run whatever the
-- model decides to test.
--
-- Public domain of the `bahs` service (Railway -> bahs -> Settings -> Networking -> Generate
-- Domain). That service is the bridge: it holds the Qwen token, so this URL is the only one
-- needed anywhere.
local API_URL = "https://bahs-production-d68f.up.railway.app"
-- The key the API requires. That is the API_KEY value from the bahs service if you set one, and
-- otherwise the QWEN_TOKEN itself -- so this is the one secret to keep straight. Leave "" only if
-- the service has neither.
local API_KEY = ""

-- Which chain answers. "agent" sends the question to DeepSeek first, which plans it, and then to
-- qwen3.8-max, which writes the script with its tools from that plan; "qwen" is qwen3.8-max on its
-- own; "deepseek" is DeepSeek on its own. The service refuses a mode whose credential it does not
-- have, so a wrong one here says so instead of being answered by another model. Each ask carries
-- the mode, so it can change between questions without starting a new chat.
local MODE = "agent"
local MODES = { "agent", "qwen", "deepseek" }

-- Three things that take a while to get right here:
--   * Roblox reads an HTTP response in one piece, so it cannot follow the NDJSON stream the page
--     uses. A turn is started with /chat/stream (which returns at once), then polled on
--     /chat/result until it says done -- which is what makes a turn with tool rounds survivable
--     instead of one request that dies at the executor's timeout.
--   * The conversation is kept here between asks, and one session id goes with every turn: the
--     server turns that into one continuing upstream chat instead of a new chat per question.
--     The service puts "Hy kanha" in front of each question on its way out.
--   * "listen" is what makes the model's own testing real: it polls /agent/pull for scripts the
--     model queued with its run_script tool, runs them here, and posts the prints and the
--     traceback back with /agent/push. Turn it on and the model can find its own bugs.
local messages = {}
local SESSION = tostring(math.floor(os.clock() * 1000000)) .. tostring(os.time())
local CLIENT = "in-game-" .. tostring(math.random(100000, 999999))

local HttpService = game:GetService("HttpService")
local Players = game:GetService("Players")
local LocalPlayer = Players.LocalPlayer

local function headers()
    local h = {["Content-Type"] = "application/json"}
    if API_KEY ~= "" then h["X-API-Key"] = API_KEY end
    return h
end

local function post(path, body)
    local r = request({
        Url = API_URL .. path,
        Method = "POST",
        Headers = headers(),
        Body = HttpService:JSONEncode(body)
    })
    if r.StatusCode >= 400 then
        error("API " .. tostring(r.StatusCode) .. ": " .. tostring(r.Body), 0)
    end
    return HttpService:JSONDecode(r.Body)
end

local function get(path)
    local r = request({Url = API_URL .. path, Method = "GET", Headers = headers()})
    if r.StatusCode >= 400 then
        error("API " .. tostring(r.StatusCode) .. ": " .. tostring(r.Body), 0)
    end
    return HttpService:JSONDecode(r.Body)
end

-- Start the turn and follow it. `onStep` is called with each phase note, so "luau_check --
-- running (12s)" is visible instead of one silent wait.
local function ask(question, onStep)
    table.insert(messages, {role = "user", content = question})
    local ok, result = pcall(function()
        local started = post("/chat/stream",
            {messages = messages, session = SESSION, mode = MODE})
        local job = started.job
        -- Wall clock, not os.clock(): that one counts the CPU this script has used, which barely
        -- moves while it waits, so it would never reach the deadline. Up to AGENT_ROUNDS tool
        -- rounds with no ceiling on any of them, so this is a courtesy rather than a limit.
        local deadline = os.time() + 3600
        while os.time() < deadline do
            local d = get("/chat/result/" .. tostring(job))
            if d.status == "done" then return d end
            if d.status == "error" then error(d.error or "the turn failed", 0) end
            if onStep then
                onStep(tostring(d.note or d.phase or "working") ..
                       " (call " .. tostring(d.calls or 1) .. ")", tonumber(d.elapsed) or 0)
            end
            task.wait(1)
        end
        error("timed out waiting for the turn", 0)
    end)
    if not ok then
        -- Drop the question that failed, so the next ask is not a broken conversation.
        table.remove(messages)
        error(result, 0)
    end
    table.insert(messages, {role = "assistant", content = result.text or ""})
    return result
end

-- Run one script the model asked for, and collect what it printed and any traceback. This runs
-- in this client's executor, which is the point: it is the only way anything can say a script
-- actually works.
local function runCaptured(code)
    local out = {}
    local function capture(...)
        local args = table.pack(...)
        local parts = {}
        for i = 1, args.n do parts[#parts + 1] = tostring(args[i]) end
        if #out < 200 then table.insert(out, table.concat(parts, " ")) end
    end
    local printed = function() return table.concat(out, "\n") end
    local chunk, compileErr = loadstring(code)
    if not chunk then
        return false, "", "compile error: " .. tostring(compileErr)
    end
    local base = (getgenv and getgenv()) or (getfenv and getfenv()) or _G
    local env = setmetatable({print = capture, warn = capture}, {__index = base})
    if setfenv then pcall(setfenv, chunk, env) end
    local ok, err = xpcall(chunk, function(e) return debug.traceback(tostring(e), 2) end)
    local detail = ""
    if not ok then detail = tostring(err) end
    if #detail > 4000 then detail = string.sub(detail, 1, 4000) .. "\n[...cut...]" end
    local text = printed()
    if #text > 4000 then text = string.sub(text, 1, 4000) .. "\n[...cut...]" end
    return ok, text, detail
end

local gui = Instance.new("ScreenGui")
gui.ResetOnSpawn = false
gui.Parent = LocalPlayer:WaitForChild("PlayerGui")

local main = Instance.new("Frame")
main.Size = UDim2.new(0, 400, 0, 300)
main.Position = UDim2.new(0.5, -200, 0.5, -150)
main.BackgroundColor3 = Color3.fromRGB(25, 25, 35)
main.BorderSizePixel = 0
main.Active = true
main.Draggable = true
main.Parent = gui

local corner = Instance.new("UICorner")
corner.CornerRadius = UDim.new(0, 8)
corner.Parent = main

local input = Instance.new("TextBox")
input.Size = UDim2.new(1, -16, 0, 50)
input.Position = UDim2.new(0, 8, 0, 8)
input.BackgroundColor3 = Color3.fromRGB(20, 20, 30)
input.TextColor3 = Color3.new(1, 1, 1)
input.PlaceholderText = "ask kanha..."
input.PlaceholderColor3 = Color3.fromRGB(100, 100, 120)
input.Text = ""
input.MultiLine = true
input.ClearTextOnFocus = false
input.Font = Enum.Font.Code
input.TextSize = 13
input.TextXAlignment = Enum.TextXAlignment.Left
input.TextYAlignment = Enum.TextYAlignment.Top
input.Parent = main

local inputCorner = Instance.new("UICorner")
inputCorner.CornerRadius = UDim.new(0, 6)
inputCorner.Parent = input

local inputPad = Instance.new("UIPadding")
inputPad.PaddingTop = UDim.new(0, 6)
inputPad.PaddingLeft = UDim.new(0, 6)
inputPad.Parent = input

local output = Instance.new("TextBox")
output.Size = UDim2.new(1, -16, 1, -110)
output.Position = UDim2.new(0, 8, 0, 64)
output.BackgroundColor3 = Color3.fromRGB(15, 15, 25)
output.TextColor3 = Color3.fromRGB(140, 220, 140)
output.Text = "-- the answer lands here --"
output.MultiLine = true
output.ClearTextOnFocus = false
output.TextEditable = false
output.Font = Enum.Font.Code
output.TextSize = 12
output.TextXAlignment = Enum.TextXAlignment.Left
output.TextYAlignment = Enum.TextYAlignment.Top
output.Parent = main

local outputCorner = Instance.new("UICorner")
outputCorner.CornerRadius = UDim.new(0, 6)
outputCorner.Parent = output

local outputPad = Instance.new("UIPadding")
outputPad.PaddingTop = UDim.new(0, 6)
outputPad.PaddingLeft = UDim.new(0, 6)
outputPad.Parent = output

local lastAnswer
local lastTool
local listening = false

local function makeBtn(text, x, color, callback)
    local b = Instance.new("TextButton")
    b.Size = UDim2.new(0.139, -3, 0, 28)
    b.Position = UDim2.new(x, 0, 1, -36)
    b.BackgroundColor3 = color
    b.Text = text
    b.TextColor3 = Color3.new(1, 1, 1)
    b.Font = Enum.Font.Code
    b.TextSize = 11
    b.Parent = main
    local bc = Instance.new("UICorner")
    bc.CornerRadius = UDim.new(0, 6)
    bc.Parent = b
    b.MouseButton1Click:Connect(callback)
    return b
end

local function showStep(note, elapsed)
    output.Text = "-- " .. note .. " (" .. tostring(math.floor(elapsed)) .. "s)"
end

makeBtn("ask", 0.002, Color3.fromRGB(60, 120, 200), function()
    local question = input.Text
    if question == "" then return end
    input.Text = ""
    output.Text = "-- kanha is working..."
    local ok, result = pcall(ask, question, showStep)
    if not ok then
        output.Text = "-- error: " .. tostring(result)
        return
    end
    lastAnswer = result.text or ""
    lastTool = result.tool
    -- The plan, when there was one: it is what the script was written from, and it is the part of
    -- an agent turn a reader may want to disagree with. Shown with the answer, prefixed.
    if result.plan and result.plan ~= "" then
        output.Text = "-- plan (" .. MODE .. ")\n" .. result.plan .. "\n\n-- script\n" .. lastAnswer
    else
        output.Text = lastAnswer
    end
end)

local modeBtn
modeBtn = makeBtn("mode: " .. MODE, 0.142, Color3.fromRGB(120, 90, 180), function()
    -- The chain the next question takes. Each press moves to the next mode; the answer says which
    -- one is now in force, so it is never a guess which model wrote what.
    local index = 1
    for i, name in ipairs(MODES) do
        if name == MODE then index = i end
    end
    MODE = MODES[index % #MODES + 1]
    modeBtn.Text = "mode: " .. MODE
    output.Text = "-- the next question goes through: " .. MODE
end)

makeBtn("execute", 0.282, Color3.fromRGB(80, 160, 80), function()
    if not lastAnswer then return end
    -- The answer is a chat reply, so only run what looks like a script: a fenced block if there
    -- is one, otherwise the whole answer.
    local code = lastAnswer:match("```[%w]*\n(.-)```") or lastAnswer
    local fn, err = loadstring(code)
    if not fn then
        output.Text = "-- loadstring error: " .. tostring(err)
        return
    end
    local ok, runErr = pcall(fn)
    if not ok then
        output.Text = "-- runtime error: " .. tostring(runErr) .. "\n\n" .. code
    end
end)

makeBtn("tools", 0.422, Color3.fromRGB(150, 110, 60), function()
    -- What the model did to its own work this turn: which tools it called, and what came back.
    if lastTool and lastTool ~= "" then
        output.Text = "-- tools\n" .. lastTool
    else
        output.Text = "-- no tool call in the last turn"
    end
end)

makeBtn("listen", 0.562, Color3.fromRGB(90, 130, 180), function()
    if listening then
        listening = false
        output.Text = "-- stopped listening: the model can no longer run scripts here"
        return
    end
    listening = true
    output.Text = "-- listening on /agent/pull as " .. CLIENT ..
                  "; run_script can now run and read the result"
    task.spawn(function()
        while listening do
            local ok, task = pcall(get, "/agent/pull?client=" .. CLIENT)
            if ok and task and task.run and task.run ~= "" then
                output.Text = "-- running the model's script (" .. tostring(task.run) .. ")"
                local ran, printed, err = runCaptured(task.script or "")
                pcall(post, "/agent/push", {
                    run = task.run,
                    ok = ran,
                    output = printed or "",
                    error = err or ""
                })
                output.Text = ran and ("-- ran clean\n" .. (printed or ""))
                    or ("-- failed\n" .. (err or "") .. "\n" .. (printed or ""))
            end
            task.wait(1)
        end
    end)
end)

makeBtn("new chat", 0.702, Color3.fromRGB(70, 70, 110), function()
    -- A new session, so the model starts a new chat on its side too.
    messages = {}
    SESSION = tostring(math.floor(os.clock() * 1000000)) .. tostring(os.time())
    lastAnswer = nil
    lastTool = nil
    output.Text = "-- new chat: nothing from before is sent, and a new session starts"
end)

makeBtn("copy", 0.842, Color3.fromRGB(40, 110, 140), function()
    if not lastAnswer then return end
    if setclipboard then setclipboard(lastAnswer) end
end)
