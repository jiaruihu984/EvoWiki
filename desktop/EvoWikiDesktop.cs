// Build with scripts/build_desktop.py. No dynamic code, downloads or embedded services.
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.Drawing.Drawing2D;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Net;
using System.Security.Cryptography;
using System.Reflection;
using System.Text;
using System.Text.RegularExpressions;
using System.Threading;
using System.Threading.Tasks;
using System.Web.Script.Serialization;
using System.Windows.Forms;

internal static class EvoWikiDesktop
{
    [STAThread]
    private static void Main(string[] args)
    {
        Application.EnableVisualStyles();
        Application.SetCompatibleTextRenderingDefault(false);
        string root = Path.GetFullPath(AppDomain.CurrentDomain.BaseDirectory).TrimEnd(Path.DirectorySeparatorChar);
        string language = Environment.GetEnvironmentVariable("EVOWIKI_CLI_LANGUAGE");
        if (language != "zh" && language != "en")
            language = CultureInfo.CurrentCulture.Name.StartsWith("zh", StringComparison.OrdinalIgnoreCase) ? "zh" : "en";
        for (int i = 0; i < args.Length; i++)
        {
            if (args[i] == "--language" && i + 1 < args.Length && (args[i + 1] == "zh" || args[i + 1] == "en")) language = args[i + 1];
            if (args[i] == "--language=zh" || args[i] == "--language=en") language = args[i].Substring(11);
        }
        Environment.SetEnvironmentVariable("EVOWIKI_CLI_LANGUAGE", language);
        string identity;
        using (var hash = SHA256.Create())
            identity = BitConverter.ToString(hash.ComputeHash(Encoding.UTF8.GetBytes(root.ToLowerInvariant()))).Replace("-", "");
        bool created;
        using (var mutex = new Mutex(true, "Local\\EvoWikiDesktop-" + identity, out created))
        {
            if (!created)
            {
                MessageBox.Show(language == "zh" ? "EvoWiki 已在右下角托盘中运行。" : "EvoWiki is already in the notification area.", "EvoWiki");
                return;
            }
            try
            {
                if (!File.Exists(Path.Combine(root, "scripts", "evowiki_run.py")))
                    throw new InvalidOperationException(language == "zh" ? "请将 EvoWiki.exe 保留在仓库根目录。" : "Keep EvoWiki.exe in the repository root.");
                using (var desktop = new DesktopContext(root, args, language == "zh")) Application.Run(desktop);
            }
            catch (Exception error) { MessageBox.Show(error.Message, "EvoWiki", MessageBoxButtons.OK, MessageBoxIcon.Error); }
            finally { mutex.ReleaseMutex(); }
        }
    }

    private sealed class DesktopContext : ApplicationContext
    {
        private readonly string root, runDirectory, log;
        private readonly string[] launcherArguments;
        private readonly bool chinese;
        private readonly NotifyIcon icon;
        private readonly Icon runningIcon, stoppedIcon;
        private readonly ContextMenuStrip menu;
        private readonly System.Windows.Forms.Timer timer;
        private Process process;
        private Task<string> output, error;
        private string operation, nextAction, lastStatus;
        private bool exitAfterStop;
        private int servicePort;
        private Task<bool> serviceHealth;
        private DateTime nextHealthCheck = DateTime.MinValue;

        internal DesktopContext(string root, string[] args, bool chinese)
        {
            this.root = root; this.launcherArguments = args; this.chinese = chinese;
            runDirectory = Path.Combine(root, ".evowiki-run");
            Directory.CreateDirectory(runDirectory);
            log = Path.Combine(runDirectory, "desktop.log");
            lastStatus = Text("EvoWiki is starting.", "EvoWiki 正在启动。");
            runningIcon = LoadIcon("EvoWiki.Running.ico");
            stoppedIcon = LoadIcon("EvoWiki.Stopped.ico");
            icon = new NotifyIcon { Icon = stoppedIcon, Text = "EvoWiki" };
            menu = new ProductMenu();
            Item("Start service", "启动服务", delegate { Begin("up"); });
            Item("Stop service", "停止服务", delegate { Begin("down"); });
            Item("Restart service", "重启服务", delegate {
                if (Busy()) return; nextAction = "up"; Begin("down");
            });
            menu.Items.Add(new ToolStripSeparator());
            Item("Open vault in Obsidian", "在 Obsidian 打开笔记库", OpenVault);
            Item("Refresh status", "刷新状态", delegate { Begin("status"); });
            Item("Show last status", "查看最近状态", ShowStatus);
            menu.Items.Add(new ToolStripSeparator());
            Item("Open desktop log", "查看托盘日志", delegate { OpenLog(log); });
            Item("Open backend log", "查看后台日志", delegate { OpenLog(Path.Combine(runDirectory, "backend.log")); });
            menu.Items.Add(new ToolStripSeparator());
            Item("Stop service and exit", "停止服务并退出", delegate {
                if (Busy()) return; exitAfterStop = true; Begin("down");
            });
            icon.ContextMenuStrip = menu;
            icon.DoubleClick += delegate { ShowStatus(); };
            icon.Visible = true;
            timer = new System.Windows.Forms.Timer { Interval = 300 };
            timer.Tick += delegate { Poll(); };
            timer.Start();
            Begin("up");
        }

        private string Text(string en, string zh) { return chinese ? zh : en; }
        private string ActionName(string action)
        {
            return action == "up" ? Text("Start service", "启动服务") : action == "down" ? Text("Stop service", "停止服务") : Text("Check status", "检查状态");
        }
        private void Item(string en, string zh, Action action)
        {
            var item = new ToolStripMenuItem(Text(en, zh), null, delegate { action(); });
            item.Padding = new Padding(12, 8, 20, 8);
            menu.Items.Add(item);
        }
        private void Notify(string text, bool failed)
        {
            icon.BalloonTipTitle = "EvoWiki"; icon.BalloonTipText = text;
            icon.BalloonTipIcon = failed ? ToolTipIcon.Error : ToolTipIcon.Info; icon.ShowBalloonTip(6000);
        }
        private bool Busy()
        {
            if (process == null) return false;
            Notify(Text("Wait for the current operation to finish.", "请等待当前操作结束。"), false); return true;
        }
        private void ShowStatus()
        {
            var form = new Form { Text = Text("EvoWiki status", "EvoWiki 状态"), Size = new Size(760, 480) };
            form.Controls.Add(new TextBox { Multiline = true, ReadOnly = true, ScrollBars = ScrollBars.Both, Dock = DockStyle.Fill, Text = lastStatus });
            form.Show(); form.Activate();
        }
        private void OpenLog(string path)
        {
            if (File.Exists(path)) Process.Start(new ProcessStartInfo("notepad.exe", Quote(path)) { UseShellExecute = true });
            else Notify(Text("The log has not been created yet.", "日志尚未创建。"), false);
        }
        private Dictionary<string, object> OwnedBackend()
        {
            string path = Path.Combine(runDirectory, "state.json");
            if (!File.Exists(path)) return null;
            var state = new JavaScriptSerializer().Deserialize<Dictionary<string, object>>(File.ReadAllText(path, Encoding.UTF8));
            object value;
            if (!state.TryGetValue("backend", out value)) return null;
            var backend = value as Dictionary<string, object>;
            if (backend == null || !backend.TryGetValue("instance_root", out value) ||
                !String.Equals(Path.GetFullPath(Convert.ToString(value)).TrimEnd(Path.DirectorySeparatorChar), root, StringComparison.OrdinalIgnoreCase)) return null;
            if (backend.TryGetValue("port", out value)) servicePort = Convert.ToInt32(value, CultureInfo.InvariantCulture);
            return backend;
        }
        private void OpenVault()
        {
            try
            {
                var backend = OwnedBackend();
                if (backend == null || !backend.ContainsKey("vault")) throw new InvalidOperationException();
                Process.Start(new ProcessStartInfo("obsidian://open?path=" + Uri.EscapeDataString(Convert.ToString(backend["vault"]))) { UseShellExecute = true });
            }
            catch { Notify(Text("Start this instance first; its configured vault is unavailable.", "请先启动此实例，暂时无法确定它配置的笔记库。"), true); }
        }
        private void Begin(string action)
        {
            if (Busy()) return;
            try
            {
                var args = new List<string> { "run", "--isolated", "--no-project", "--with", "python-dotenv==1.2.4", "python", Path.Combine(root, "scripts", "evowiki_run.py"), action };
                if (action == "up") args.AddRange(launcherArguments);
                else
                {
                    var backend = OwnedBackend();
                    if (backend != null && backend.ContainsKey("port")) servicePort = Convert.ToInt32(backend["port"], CultureInfo.InvariantCulture);
                    if (servicePort > 0) { args.Add("--port"); args.Add(servicePort.ToString(CultureInfo.InvariantCulture)); }
                }
                if (action == "down") args.Add("--db");
                var info = new ProcessStartInfo("uv", String.Join(" ", args.Select(Quote))) {
                    WorkingDirectory = root, UseShellExecute = false, CreateNoWindow = true,
                    RedirectStandardOutput = true, RedirectStandardError = true,
                    StandardOutputEncoding = Encoding.UTF8, StandardErrorEncoding = Encoding.UTF8
                };
                info.EnvironmentVariables["PYTHONUTF8"] = "1";
                info.EnvironmentVariables.Remove("VIRTUAL_ENV");
                info.EnvironmentVariables["PYTHONIOENCODING"] = "utf-8";
                info.EnvironmentVariables["EVOWIKI_NO_PAUSE"] = "1";
                info.EnvironmentVariables["UV_HTTP_TIMEOUT"] = "60";
                info.EnvironmentVariables["UV_HTTP_RETRIES"] = "2";
                WriteLog(action + " started");
                process = Process.Start(info);
                output = process.StandardOutput.ReadToEndAsync(); error = process.StandardError.ReadToEndAsync();
                operation = action;
                icon.Text = "EvoWiki: " + ActionName(action);
                lastStatus = ActionName(action) + Text(" in progress. Downloads can take several minutes.", "中。首次下载可能需要几分钟，请稍候。");
            }
            catch (Exception failure) { Fail(failure.Message); }
        }
        private void Poll()
        {
            PollHealth();
            if (process == null) return;
            try
            {
                if (!process.HasExited || !output.IsCompleted || !error.IsCompleted) return;
                int code = process.ExitCode;
                string result = Protect(output.Result + Environment.NewLine + error.Result);
                process.Dispose(); process = null;
                nextHealthCheck = DateTime.MinValue;
                lastStatus = ActionName(operation) + Text(" finished; exit code ", "结束，退出码 ") + code + Environment.NewLine + result;
                WriteLog(lastStatus);
                if (code != 0) { Fail(lastStatus); return; }
                if (exitAfterStop) { ExitThread(); return; }
                if (nextAction != null) { string next = nextAction; nextAction = null; Begin(next); return; }
                if (operation == "status") ShowStatus();
                else Notify(ActionName(operation) + Text(" completed. Right-click the tray icon for controls.", "完成。右键点击托盘图标可查看操作菜单。"), false);
            }
            catch (Exception failure) { Fail(failure.Message); }
        }
        private void Fail(string detail)
        {
            nextAction = null; exitAfterStop = false;
            if (process != null && process.HasExited) { process.Dispose(); process = null; }
            lastStatus = Protect(detail);
            try { WriteLog(lastStatus); } catch { }
            icon.Text = Text("EvoWiki: operation failed", "EvoWiki：操作失败");
            Notify(Text("Operation failed. Open status or desktop log; the tray remains available.", "操作失败，托盘将保留。请查看状态或托盘日志。"), true);
            ShowStatus();
        }
        private static Icon LoadIcon(string name)
        {
            using (var stream = Assembly.GetExecutingAssembly().GetManifestResourceStream(name))
            using (var original = new Icon(stream, SystemInformation.SmallIconSize))
                return (Icon)original.Clone();
        }
        private bool BackendRunning()
        {
            try
            {
                var backend = OwnedBackend();
                if (backend == null || !backend.ContainsKey("port")) return false;
                int port = Convert.ToInt32(backend["port"], CultureInfo.InvariantCulture);
                if (port < 1 || port > 65535) return false;
                var request = (HttpWebRequest)WebRequest.Create("http://127.0.0.1:" + port + "/health");
                request.Proxy = null;
                request.AllowAutoRedirect = false;
                request.Timeout = 1500;
                request.ReadWriteTimeout = 1500;
                using (var response = request.GetResponse())
                using (var reader = new StreamReader(response.GetResponseStream(), Encoding.UTF8))
                {
                    var health = new JavaScriptSerializer().Deserialize<Dictionary<string, object>>(reader.ReadToEnd());
                    object value;
                    return health.TryGetValue("ok", out value) && value is bool && (bool)value &&
                        health.TryGetValue("instance_root", out value) &&
                        String.Equals(Path.GetFullPath(Convert.ToString(value)).TrimEnd(Path.DirectorySeparatorChar), root, StringComparison.OrdinalIgnoreCase);
                }
            }
            catch { return false; }
        }
        private void PollHealth()
        {
            if (serviceHealth != null)
            {
                if (!serviceHealth.IsCompleted) return;
                bool running = serviceHealth.Status == TaskStatus.RanToCompletion && serviceHealth.Result;
                icon.Icon = running ? runningIcon : stoppedIcon;
                if (process == null) icon.Text = running ? Text("EvoWiki: running", "EvoWiki：运行中") : Text("EvoWiki: stopped", "EvoWiki：已停止");
                serviceHealth = null;
            }
            if (DateTime.UtcNow < nextHealthCheck) return;
            nextHealthCheck = DateTime.UtcNow.AddSeconds(5);
            serviceHealth = Task.Run((Func<bool>)BackendRunning);
        }
        private static string Protect(string text)
        {
            text = Regex.Replace(text, @"(?im)[^\r\n]*(?:api[_-]?key|authorization|password|secret|token)[^\r\n]*", "[credential-bearing line omitted]");
            return Regex.Replace(text, @"(?i)(https?://)[^\s/@:]+:[^\s/@]+@", "$1[redacted]@");
        }
        private void WriteLog(string text) { File.AppendAllText(log, "[" + DateTime.Now.ToString("s") + "] " + Protect(text) + Environment.NewLine, Encoding.UTF8); }
        protected override void Dispose(bool disposing)
        {
            if (disposing) { timer.Stop(); timer.Dispose(); icon.Visible = false; icon.Dispose(); runningIcon.Dispose(); stoppedIcon.Dispose(); menu.Dispose(); if (process != null) process.Dispose(); }
            base.Dispose(disposing);
        }
    }
    // The plugin's neutral palette and rounded controls, with native menu navigation.
    private sealed class ProductMenu : ContextMenuStrip
    {
        internal ProductMenu()
        {
            ShowImageMargin = false;
            ShowCheckMargin = false;
            BackColor = Color.White;
            ForeColor = Color.FromArgb(34, 34, 34);
            Font = new Font("Microsoft YaHei UI", 10, FontStyle.Regular);
            Padding = new Padding(6);
            Renderer = new ProductMenuRenderer();
        }
        protected override void OnSizeChanged(EventArgs e)
        {
            base.OnSizeChanged(e);
            if (Width < 2 || Height < 2) return;
            using (var path = Rounded(new Rectangle(0, 0, Width, Height), 10))
            {
                var previous = Region;
                Region = new Region(path);
                if (previous != null) previous.Dispose();
            }
        }
    }
    private sealed class ProductMenuRenderer : ToolStripProfessionalRenderer
    {
        internal ProductMenuRenderer() { RoundedEdges = false; }
        protected override void OnRenderToolStripBackground(ToolStripRenderEventArgs e)
        {
            e.Graphics.Clear(Color.White);
        }
        protected override void OnRenderToolStripBorder(ToolStripRenderEventArgs e)
        {
            e.Graphics.SmoothingMode = SmoothingMode.AntiAlias;
            using (var path = Rounded(new Rectangle(0, 0, e.ToolStrip.Width - 1, e.ToolStrip.Height - 1), 10))
            using (var pen = new Pen(Color.FromArgb(224, 224, 224))) e.Graphics.DrawPath(pen, path);
        }
        protected override void OnRenderMenuItemBackground(ToolStripItemRenderEventArgs e)
        {
            if (!e.Item.Selected || !e.Item.Enabled) return;
            e.Graphics.SmoothingMode = SmoothingMode.AntiAlias;
            using (var path = Rounded(new Rectangle(0, 0, e.Item.Width - 1, e.Item.Height - 1), 6))
            using (var brush = new SolidBrush(Color.FromArgb(246, 246, 246))) e.Graphics.FillPath(brush, path);
        }
        protected override void OnRenderSeparator(ToolStripSeparatorRenderEventArgs e)
        {
            using (var pen = new Pen(Color.FromArgb(235, 235, 235)))
                e.Graphics.DrawLine(pen, 12, e.Item.Height / 2, e.Item.Width - 12, e.Item.Height / 2);
        }
    }
    private static GraphicsPath Rounded(Rectangle bounds, int radius)
    {
        int diameter = Math.Min(radius * 2, Math.Min(bounds.Width, bounds.Height));
        var path = new GraphicsPath();
        path.AddArc(bounds.X, bounds.Y, diameter, diameter, 180, 90);
        path.AddArc(bounds.Right - diameter, bounds.Y, diameter, diameter, 270, 90);
        path.AddArc(bounds.Right - diameter, bounds.Bottom - diameter, diameter, diameter, 0, 90);
        path.AddArc(bounds.X, bounds.Bottom - diameter, diameter, diameter, 90, 90);
        path.CloseFigure();
        return path;
    }
    // Windows argv quoting, including quotes and trailing backslashes. No shell.
    private static string Quote(string value)
    {
        var result = new StringBuilder("\""); int slashes = 0;
        foreach (char c in value)
        {
            if (c == '\\') { slashes++; continue; }
            if (c == '"') result.Append('\\', slashes * 2 + 1).Append('"');
            else result.Append('\\', slashes).Append(c);
            slashes = 0;
        }
        return result.Append('\\', slashes * 2).Append('"').ToString();
    }
}
