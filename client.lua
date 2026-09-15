local HttpService = game:GetService("HttpService")
local Players = game:GetService("Players")
local LocalPlayer = Players.LocalPlayer

-- Public domain of the `bahs` service (Railway -> bahs -> Settings -> Networking ->
-- Generate Domain). That service is the bridge: it holds the Qwen token and calls Qwen,
-- so this URL is the only one needed anywhere.
local API_URL = "https://bahs-production-d68f.up.railway.app"
-- Paste the API_KEY value from the bahs service here. Leave "" if you never set one.
local API_KEY = ""

-- The conversation is kept here between asks, so a follow-up ("make it faster") lands in
-- the same chat the model has already been answering in. The service puts "Hy kanha" in
-- front of each question on its way out.
local messages = {}

local function headers()
    local h = {["Content-Type"] = "application/json"}
    if API_KEY ~= "" then h["X-API-Key"] = API_KEY end
    return h
end

local function ask(question)
    table.insert(messages, {role = "user", content = question})
    local r = request({
        Url = API_URL .. "/v1/chat/completions",
        Method = "POST",
        Headers = headers(),
        Body = HttpService:JSONEncode({messages = messages, stream = false})
    })
    if r.StatusCode >= 400 then
        -- Drop the question that failed, so the next ask is not a broken conversation.
        table.remove(messages)
        error("API " .. tostring(r.StatusCode) .. ": " .. tostring(r.Body), 0)
    end
    local data = HttpService:JSONDecode(r.Body)
    local choice = data.choices and data.choices[1]
    local text = (choice and choice.message and choice.message.content) or ""
    table.insert(messages, {role = "assistant", content = text})
    return text
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

local function makeBtn(text, x, color, callback)
    local b = Instance.new("TextButton")
    b.Size = UDim2.new(0.24, -4, 0, 28)
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

makeBtn("ask", 0.01, Color3.fromRGB(60, 120, 200), function()
    local question = input.Text
    if question == "" then return end
    input.Text = ""
    output.Text = "-- kanha is thinking..."
    local ok, text = pcall(ask, question)
    if not ok then
        output.Text = "-- error: " .. tostring(text)
        return
    end
    lastAnswer = text
    output.Text = text
end)

makeBtn("execute", 0.26, Color3.fromRGB(80, 160, 80), function()
    if not lastAnswer then return end
    -- The answer is a chat reply, so only run what looks like a script: a fenced block
    -- if there is one, otherwise the whole answer.
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

makeBtn("new chat", 0.51, Color3.fromRGB(70, 70, 110), function()
    messages = {}
    lastAnswer = nil
    output.Text = "-- new chat: nothing from before is sent"
end)

makeBtn("copy", 0.76, Color3.fromRGB(40, 110, 140), function()
    if not lastAnswer then return end
    if setclipboard then setclipboard(lastAnswer) end
end)
