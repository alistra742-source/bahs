local HttpService = game:GetService("HttpService")
local Players = game:GetService("Players")
local LocalPlayer = Players.LocalPlayer

-- Public domain of the `bahs` service (Railway -> bahs -> Settings -> Networking ->
-- Generate Domain). That service is the bridge: it holds the Qwen token and the reviewer
-- key, so this URL is the only one needed anywhere.
local API_URL = "https://bahs-production-d68f.up.railway.app"
-- The key the API requires. That is the API_KEY value from the bahs service if you set one,
-- and otherwise the QWEN_TOKEN itself -- so this is the one secret to keep straight. Leave
-- "" only if the service has neither.
local API_KEY = ""

-- Things that take a while to get right here:
--   * Roblox reads an HTTP response in one piece, so it cannot follow the NDJSON stream the
--     page uses. A turn is started with /chat/stream (which returns at once), then polled on
--     /chat/result until it says done -- that is what makes a chain of three model calls
--     survivable instead of one request that dies at the executor's timeout.
--   * The conversation is kept here between asks, so a follow-up ("make it faster") lands in
--     the same chat the model has already been answering in. The service puts "Hy kanha" in
--     front of each question on its way out.
local messages = {}

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

-- Start the chain and follow it. `onStep` is called with each phase note so the caller can
-- show "deepseek-v4-flash writing its own version (44s)" instead of a silent wait.
local function ask(question, onStep)
    table.insert(messages, {role = "user", content = question})
    local ok, result = pcall(function()
        local started = post("/chat/stream", {messages = messages})
        local job = started.job
        local deadline = os.clock() + 900
        while os.clock() < deadline do
            local r = request({
                Url = API_URL .. "/chat/result/" .. tostring(job),
                Method = "GET",
                Headers = headers()
            })
            if r.StatusCode >= 400 then
                error("API " .. tostring(r.StatusCode) .. ": " .. tostring(r.Body), 0)
            end
            local d = HttpService:JSONDecode(r.Body)
            if d.status == "done" then return d end
            if d.status == "error" then error(d.error or "the chain failed", 0) end
            if onStep then
                onStep(tostring(d.note or d.phase or "working"), tonumber(d.elapsed) or 0)
            end
            task.wait(1)
        end
        error("timed out waiting for the chain", 0)
    end)
    if not ok then
        -- Drop the question that failed, so the next ask is not a broken conversation.
        table.remove(messages)
        error(result, 0)
    end
    table.insert(messages, {role = "assistant", content = result.text or ""})
    return result
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
local lastReview

local function makeBtn(text, x, color, callback)
    local b = Instance.new("TextButton")
    b.Size = UDim2.new(0.19, -4, 0, 28)
    b.Position = UDim2.new(x, 0, 1, -36)
    b.BackgroundColor3 = color
    b.Text = text
    b.TextColor3 = Color3.new(1, 1, 1)
    b.Font = Enum.Font.Code
    b.TextSize = 12
    b.Parent = main
    local bc = Instance.new("UICorner")
    bc.CornerRadius = UDim.new(0, 6)
    bc.Parent = b
    b.MouseButton1Click:Connect(callback)
end

local function showStep(note, elapsed)
    output.Text = "-- " .. note .. " (" .. tostring(math.floor(elapsed)) .. "s)"
end

makeBtn("ask", 0.002, Color3.fromRGB(60, 120, 200), function()
    local question = input.Text
    if question == "" then return end
    input.Text = ""
    output.Text = "-- kanha is drafting..."
    local ok, result = pcall(ask, question, showStep)
    if not ok then
        output.Text = "-- error: " .. tostring(result)
        return
    end
    lastAnswer = result.text or ""
    lastReview = result.review
    output.Text = lastAnswer
end)

makeBtn("execute", 0.202, Color3.fromRGB(80, 160, 80), function()
    if not lastAnswer then return end
    -- The answer is a chat reply, so only run what looks like a script: a fenced block if
    -- there is one, otherwise the whole answer.
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

makeBtn("review", 0.402, Color3.fromRGB(150, 110, 60), function()
    if lastReview and lastReview ~= "" then
        output.Text = "-- " .. lastReview
    else
        output.Text = "-- no reviewer answered for the last turn"
    end
end)

makeBtn("new chat", 0.602, Color3.fromRGB(70, 70, 110), function()
    messages = {}
    lastAnswer = nil
    lastReview = nil
    output.Text = "-- new chat: nothing from before is sent"
end)

makeBtn("copy", 0.802, Color3.fromRGB(40, 110, 140), function()
    if not lastAnswer then return end
    if setclipboard then setclipboard(lastAnswer) end
end)
