local HttpService = game:GetService("HttpService")
local LocalPlayer = game:GetService("Players").LocalPlayer
-- Public domain of the `bahs` service (Railway -> bahs -> Settings -> Networking
-- -> Generate Domain). Not the ollama service: the API reaches its model internally
-- at ollama.railway.internal:11434 and never over a public URL.
local API_URL = "https://bahs-production-d68f.up.railway.app"
-- Paste the API_KEY value from the bahs service here. Leave "" if you never set one.
local API_KEY = ""

-- The API answers 401 without this header when API_KEY is set on the service.
local function headers()
    local h = {["Content-Type"] = "application/json"}
    if API_KEY ~= "" then h["X-API-Key"] = API_KEY end
    return h
end

local function generate(prompt)
    local r = request({
        Url = API_URL .. "/generate",
        Method = "POST",
        Headers = headers(),
        Body = HttpService:JSONEncode({prompt = prompt, temperature = 0.7})
    })
    if r.StatusCode >= 400 then
        error("API " .. tostring(r.StatusCode) .. ": " .. tostring(r.Body), 0)
    end
    return HttpService:JSONDecode(r.Body)
end

local function feedback(id, worked, notes)
    request({
        Url = API_URL .. "/feedback",
        Method = "POST",
        Headers = headers(),
        Body = HttpService:JSONEncode({script_id = id, worked = worked, notes = notes or ""})
    })
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
input.PlaceholderText = "describe script..."
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
output.Text = "-- code here --"
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

local lastId, lastCode

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

makeBtn("generate", 0.01, Color3.fromRGB(60, 120, 200), function()
    if input.Text == "" then return end
    output.Text = "-- generating..."
    local ok, data = pcall(generate, input.Text)
    if not ok then
        output.Text = "-- error: " .. tostring(data)
        return
    end
    lastId, lastCode = data.id, data.code
    output.Text = data.code
end)

makeBtn("execute", 0.26, Color3.fromRGB(80, 160, 80), function()
    if not lastCode then return end
    local fn, err = loadstring(lastCode)
    if not fn then
        output.Text = "-- loadstring error: " .. tostring(err)
        return
    end
    local ok, runErr = pcall(fn)
    if not ok then
        output.Text = "-- runtime error: " .. tostring(runErr) .. "\n\n" .. lastCode
    end
end)

makeBtn("works", 0.51, Color3.fromRGB(40, 140, 40), function()
    if lastId then
        feedback(lastId, true)
        output.Text = output.Text .. "\n\n-- marked as working"
    end
end)

makeBtn("broken", 0.76, Color3.fromRGB(180, 60, 60), function()
    if lastId then
        local notes = output.Text:match("error: (.-)\n") or "unknown error"
        feedback(lastId, false, notes)
        output.Text = output.Text .. "\n\n-- marked as broken"
    end
end)
