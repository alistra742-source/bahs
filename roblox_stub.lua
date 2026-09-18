--[[
A stand-in for Roblox, so ghaith.lua can be *run* and not only compiled.

verify_chain.py concatenates this file in front of the client and runs the pair with the Luau CLI
(register_checks' compiler check catches a script that will not compile; this catches a script that
compiles and then throws on its way up -- the failure that looks like a panel which never appears).
Nothing here talks to a network or to a real game: it is enough of the API for the client to build
its panel, wire its buttons and reach its last boot line, and it is deliberately strict about what
it does not know, so a client that reads a property Roblox does not have gets a nil and an error
rather than a comfortable lie.

What it covers: Instance.new for the classes the panel builds (with Parent, Name, Size, Position and
the rest behaving as properties, and GetChildren/FindFirstChild/IsA/Destroy/GetPropertyChangedSignal
as methods), the signal objects the client connects to, the services it asks for by name, one
camera with a viewport, one player with a PlayerGui, and the datatypes it builds -- UDim, UDim2,
Vector2, Color3, ColorSequence, TweenInfo -- plus Enum, task, and a TweenService whose tweens play
immediately.

Run it by hand with:
    (cat roblox_stub.lua ghaith.lua; printf '\nprint("boot: " .. STUB_STEPS(60) .. " round(s)")\n') \
        > /tmp/boot.lua && luau /tmp/boot.lua | tail -20

The last line the client prints on its way up is `Ghaith 2.0 .. <url> .. mode .. writer`, and that
line -- with no error above it -- is what the boot check looks for.
]]

local function signal()
	local handlers = {}
	local self = {}
	function self:Connect(fn)
		if type(fn) == "function" then table.insert(handlers, fn) end
		return {Disconnect = function() end, Connected = true}
	end
	function self:Once(fn) return self:Connect(fn) end
	function self:Wait() return nil end
	function self:Fire(...)
		for _, fn in ipairs(handlers) do fn(...) end
	end
	return self
end

-- Anything that reads as a signal because the client connects to it. A property read that is not
-- on this list and is not a method returns nil, exactly as it would in Roblox.
local SIGNALS = {
	Activated = true, Changed = true, ChildAdded = true, ChildRemoved = true,
	DescendantAdded = true, FocusLost = true, Focused = true, InputBegan = true,
	InputChanged = true, InputEnded = true, MessageOut = true, MouseEnter = true,
	MouseLeave = true, MouseMoved = true, OnClientEvent = true, OnClientInvoke = true,
	StateChanged = true, TouchLongPress = true, TouchPan = true, TouchPinch = true,
	TouchRotate = true, TouchSwipe = true, TouchTap = true, AncestryChanged = true,
}

local GUI_CLASSES = {
	ScreenGui = true, Frame = true, ScrollingFrame = true, TextLabel = true, TextButton = true,
	TextBox = true, ImageLabel = true, ImageButton = true, UIListLayout = true, UIGridLayout = true,
	UIPadding = true, UICorner = true, UIStroke = true, UIGradient = true, UIScale = true,
	CanvasGroup = true,
}

local DEFAULTS = {
	Size = nil, Position = nil, AnchorPoint = nil, Name = "", Visible = true, Text = "",
	AbsoluteSize = nil, AbsolutePosition = nil, AbsoluteCanvasSize = nil, CanvasPosition = nil,
	ViewportSize = nil, Enabled = true, Parent = nil, ClassName = "",
}

local METHODS = {}

local function new_instance(class)
	local instance = setmetatable({ClassName = class, Name = class, _children = {}}, {
		__index = function(self, key)
			local method = METHODS[key]
			if method then return method end
			if SIGNALS[key] then
				local made = signal()
				rawset(self, key, made)
				return made
			end
			if DEFAULTS[key] ~= nil then return DEFAULTS[key] end
			if key == "Size" or key == "Position" then return UDim2.new(0, 0, 0, 0) end
			if key == "AnchorPoint" then return Vector2.new(0, 0) end
			if key == "ViewportSize" then return Vector2.new(1280, 720) end
			if key == "AbsoluteSize" or key == "AbsoluteCanvasSize" or key == "CanvasPosition" then
				return Vector2.new(0, 0)
			end
			return nil
		end,
		__newindex = function(self, key, value)
			if key == "Parent" then
				local was = rawget(self, "Parent")
				if was and rawget(was, "_children") then
					local kids = rawget(was, "_children")
					for index = #kids, 1, -1 do
						if kids[index] == self then table.remove(kids, index) end
					end
				end
				rawset(self, "Parent", value)
				local kids = value and rawget(value, "_children")
				if kids then table.insert(kids, self) end
				return
			end
			rawset(self, key, value)
		end,
		__tostring = function(self) return rawget(self, "Name") or class end,
	})
	return instance
end

METHODS.GetChildren = function(self)
	local copy = {}
	for index, child in ipairs(rawget(self, "_children") or {}) do copy[index] = child end
	return copy
end
METHODS.GetDescendants = function(self)
	local out = {}
	for _, child in ipairs(METHODS.GetChildren(self)) do
		table.insert(out, child)
		for _, deep in ipairs(METHODS.GetDescendants(child)) do table.insert(out, deep) end
	end
	return out
end
METHODS.FindFirstChild = function(self, name)
	for _, child in ipairs(rawget(self, "_children") or {}) do
		if rawget(child, "Name") == name then return child end
	end
	return nil
end
METHODS.WaitForChild = function(self, name) return METHODS.FindFirstChild(self, name) end
METHODS.FindFirstChildOfClass = function(self, class)
	for _, child in ipairs(rawget(self, "_children") or {}) do
		if rawget(child, "ClassName") == class then return child end
	end
	return nil
end
METHODS.IsA = function(self, class)
	local mine = rawget(self, "ClassName")
	if class == mine or class == "Instance" then return true end
	if class == "GuiObject" or class == "GuiBase2d" or class == "GuiBase" then
		return GUI_CLASSES[mine] == true
	end
	return false
end
METHODS.Destroy = function(self) self.Parent = nil end
METHODS.ClearAllChildren = function(self)
	for _, child in ipairs(METHODS.GetChildren(self)) do child.Parent = nil end
end
METHODS.Clone = function(self) return new_instance(rawget(self, "ClassName")) end
METHODS.GetPropertyChangedSignal = function() return signal() end
METHODS.SetAttribute = function(self, key, value) rawset(self, key, value) end
METHODS.GetAttribute = function(self, key) return rawget(self, key) end

-- ---------------------------------------------------------------- the datatypes
UDim, UDim2, Vector2, Vector3, CFrame, Color3, ColorSequence, NumberSequence, NumberRange,
TweenInfo = {}, {}, {}, {}, {}, {}, {}, {}, {}, {}

function UDim.new(scale, offset) return {Scale = scale or 0, Offset = offset or 0} end
function UDim2.new(xs, xo, ys, yo)
	return {X = UDim.new(xs, xo), Y = UDim.new(ys, yo)}
end
function UDim2.fromScale(x, y) return UDim2.new(x, 0, y, 0) end
function UDim2.fromOffset(x, y) return UDim2.new(0, x, 0, y) end
function Vector2.new(x, y) return {X = x or 0, Y = y or 0} end
function Vector3.new(x, y, z) return {X = x or 0, Y = y or 0, Z = z or 0} end
function CFrame.new() return {Position = Vector3.new(0, 0, 0)} end
function Color3.new(r, g, b) return {R = r or 0, G = g or 0, B = b or 0} end
function Color3.fromRGB(r, g, b) return {R = (r or 0) / 255, G = (g or 0) / 255, B = (b or 0) / 255} end
function ColorSequence.new(from, to) return {from = from, to = to} end
function NumberSequence.new(one, two) return {one = one, two = two} end
function NumberRange.new(min, max) return {Min = min, Max = max} end
function TweenInfo.new(...) return {...} end

-- Every enum item is the same table each time it is asked for, so `x == Enum.Font.Code` compares
-- the way it does in Roblox rather than being true only at one call site.
local enum_cache = {}
Enum = setmetatable({}, {
	__index = function(_, family)
		local found = enum_cache[family]
		if found then return found end
		found = setmetatable({}, {
			__index = function(_, item)
				local made = {Name = item, EnumType = family}
				rawset(found, item, made)
				return made
			end,
		})
		enum_cache[family] = found
		return found
	end,
})

Instance = {new = function(class) return new_instance(class or "Instance") end}

-- task, with the one property that matters for a boot check: `spawn` does not run the function
-- where it is called, and `wait` yields rather than returning. A client that starts an animation
-- loop at load would otherwise spin here forever -- and the loop is exactly the kind of code that
-- has to be allowed to run at least once, because it is where a nil is found.
local QUEUE = {}

STUB_STEPS = function(rounds)
	for round = 1, rounds do
		local pending, QUEUE = QUEUE, {}
		if #pending == 0 then return round end
		for _, item in ipairs(pending) do
			-- A task that has already finished is skipped rather than resumed again: a deferred
			-- function that never yields is done after its first round, and resuming it a second
			-- time is the stub's own fault, not the client's.
			if coroutine.status(item.co) == "suspended" then
				local ok, err = coroutine.resume(item.co)
				if not ok then error(err, 0) end
				if coroutine.status(item.co) == "suspended" then table.insert(QUEUE, item) end
			end
		end
	end
	return rounds
end

task = setmetatable({
	wait = function()
		if coroutine.isyieldable() then coroutine.yield() end
		return 0
	end,
	spawn = function(fn)
		if type(fn) == "function" then table.insert(QUEUE, {co = coroutine.create(fn)}) end
	end,
	defer = function(fn)
		if type(fn) == "function" then table.insert(QUEUE, {co = coroutine.create(fn)}) end
	end,
	delay = function(_, fn)
		if type(fn) == "function" then table.insert(QUEUE, {co = coroutine.create(fn)}) end
	end,
	cancel = function() end,
}, {__index = function(_, key)
	return function() return nil end
end})

-- ---------------------------------------------------------------- the services
local camera = new_instance("Camera")
camera.ViewportSize = Vector2.new(1280, 720)

local player_gui = new_instance("PlayerGui")
local player = new_instance("Player")
player.Name = "Player1"
player.WaitForChild = function(_, name) return name == "PlayerGui" and player_gui or nil end

local services = {}
local function service(name)
	if services[name] then return services[name] end
	local made = new_instance(name)
	if name == "Players" then made.LocalPlayer = player end
	if name == "GuiService" then
		made.GetGuiInset = function() return Vector2.new(0, 36), Vector2.new(0, 0) end
	end
	-- A service is a child of the DataModel, which is how the client's scan finds them: it walks
	-- `game:GetChildren()` rather than trusting a list of names.
	if name ~= "Workspace" then made.Parent = game end
	if name == "HttpService" then
		-- JSON is stubbed whole: what a check wants to know is what the *client* does with an
		-- answer, and a table handed straight through answers that without a parser in the way.
		made.JSONEncode = function(_, value) return STUB_JSON_PUT(value) end
		made.JSONDecode = function(_, text) return STUB_JSON_GET(text) end
		made.RequestAsync = function(_, options) return STUB_HTTP(options) end
	end
	if name == "TweenService" then
		made.Create = function(_, object, info, goals)
			return {Play = function()
				for key, value in pairs(goals or {}) do object[key] = value end
			end, Cancel = function() end}
		end
	end
	services[name] = made
	return made
end

game = new_instance("DataModel")
game.Name = "Game"
game.GetService = function(_, name) return service(name) end

workspace = new_instance("Workspace")
workspace.Name = "Workspace"
workspace.CurrentCamera = camera
workspace.WaitForChild = function(_, name) return name == "Camera" and camera or nil end
workspace.Parent = game
-- the one service that already exists here, so asking for it hands back the one with the camera
services.Workspace = workspace

-- =====================================================================================
-- A game to scan, a service to answer, and a way to press a button
-- =====================================================================================
-- The panel above is half of what a client does; the other half is reading a game and handing it to
-- a model. These three make that half runnable here: instances enough to walk, an HTTP layer that
-- answers the client's own requests without a network, and a way to reach into the tree and fire a
-- button's Activated signal the way a player's finger would.

-- --- JSON, as a table handed through rather than written out ---
local JSON_BOX = {}
STUB_JSON_PUT = function(value)
	local id = #JSON_BOX + 1
	JSON_BOX[id] = value
	return "<stub-json:" .. id .. ">"
end
STUB_JSON_GET = function(text)
	local id = type(text) == "string" and text:match("^<stub%-json:(%d+)>$")
	return id and JSON_BOX[tonumber(id)] or nil
end

-- --- the service's answers ---
STUB_CALLS = {}
local result_round = 0
STUB_ANSWER = "print('hello from the stub')"
STUB_RESULT_ROUNDS = function() result_round = 0 end
local function reply(body) return {StatusCode = 200, Body = STUB_JSON_PUT(body)} end

STUB_HTTP = function(options)
	local url = tostring(options.Url or "")
	local path = url:gsub("^https?://[^/]+", "")
	local body = STUB_JSON_GET(options.Body)
	table.insert(STUB_CALLS, {path = path, method = options.Method, body = body,
		raw = tostring(options.Body or "")})
	if path == "/attach" then
		local bytes = body and body.data and #body.data or 0
		return reply({id = "stubattach01", name = (body and body.name) or "file",
			mime = (body and body.mime) or "", bytes = bytes, kind = "document"})
	end
	if path == "/chat/stream" then
		result_round = 0
		return reply({job = "stubjob1", model = "qwen3.8-max", mode = "agent", thinking = "thinking"})
	end
	if path:find("^/chat/result/") then
		result_round = result_round + 1
		if result_round == 1 then
			return reply({status = "running", note = "reading the dump", thoughts = "thinking about it"})
		end
		if result_round == 2 then
			return reply({status = "running", note = "writing", text = "print('partial')"})
		end
		return reply({status = "done", note = "done", text = STUB_ANSWER, plan = "one: look, two: write"})
	end
	return {StatusCode = 404, Body = STUB_JSON_PUT({detail = "no such path in the stub: " .. path})}
end

http_request = STUB_HTTP

-- --- a game to walk ---
STUB_GAME = function(parts)
	for _, child in ipairs(METHODS.GetChildren(game)) do child.Parent = nil end
	local workspace_ = service("Workspace")
	local map = new_instance("Folder")
	map.Name = "Map"
	map.Parent = workspace_
	for index = 1, parts or 0 do
		local part = new_instance("Part")
		part.Name = "Part" .. index
		part.Parent = map
	end
	local storage = service("ReplicatedStorage")
	local fire = new_instance("RemoteEvent")
	fire.Name = "Fire"
	fire.Parent = storage
	local ask = new_instance("RemoteFunction")
	ask.Name = "Ask"
	ask.Parent = storage
	local mode = new_instance("StringValue")
	mode.Name = "Mode"
	mode.Value = "test"
	mode.Parent = storage
	local util = new_instance("ModuleScript")
	util.Name = "Util"
	util.Source = "local Util = {}\nfunction Util.add(a, b)\n\treturn a + b\nend\nreturn Util\n"
	util.Parent = storage
	local server = new_instance("Script")
	server.Name = "Server"
	server.Source = "print('server')\nlocal Util = require(script.Parent.Util)\n"
	server.Parent = service("ServerScriptService")
	return parts or 0
end

-- --- reaching into the tree, the way a finger does ---
local function walk(node, out)
	for _, child in ipairs(METHODS.GetChildren(node)) do
		table.insert(out, child)
		walk(child, out)
	end
	return out
end
STUB_TREE = function() return walk(player_gui, {}) end
STUB_BUTTON = function(text)
	for _, node in ipairs(STUB_TREE()) do
		if rawget(node, "ClassName") == "TextButton" and rawget(node, "Text") == text then return node end
	end
	return nil
end
STUB_TEXTS = function()
	local out = {}
	for _, node in ipairs(STUB_TREE()) do
		local class = rawget(node, "ClassName")
		if class == "TextLabel" or class == "TextBox" then table.insert(out, tostring(rawget(node, "Text") or "")) end
	end
	return out
end
STUB_PRESS = function(text)
	local button = STUB_BUTTON(text)
	if not button then return false end
	local fired = button.Activated
	-- Driven from a coroutine of its own, so that a handler which yields (the scan does, on purpose)
	-- is not the harness's main thread wedged where it stands.
	table.insert(QUEUE, {co = coroutine.create(function() fired:Fire() end)})
	return true
end
