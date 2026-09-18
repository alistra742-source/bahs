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
