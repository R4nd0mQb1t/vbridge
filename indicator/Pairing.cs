#nullable enable
// Reads the service's pairing.json so the user never types a token. No platform references.
using System;
using System.IO;
using System.Text.Json;

namespace VBridge.Core
{
    public static class Pairing
    {
        public static string DefaultDir =>
            Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "VBridge");

        /// <summary>The service mirrors pairing.json into the ATAS X Roaming folder because ATAS X cannot
        /// see %LocalAppData% from inside an indicator; that copy is tried first.</summary>
        public static string[] SearchDirs => new[]
        {
            Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData), "ATAS X", "VBridge"),
            DefaultDir,
        };

        public static bool TryReadAny(out string token, out int port, out string dirUsed)
        {
            var errors = new System.Collections.Generic.List<string>();
            foreach (var dir in SearchDirs)
            {
                if (TryRead(dir, out token, out port))
                {
                    dirUsed = dir;
                    return true;
                }
                errors.Add(LastError);
            }
            LastError = string.Join(" | ", errors);
            token = "";
            port = 0;
            dirUsed = "";
            return false;
        }

        /// <summary>Why the last TryRead returned false (for the indicator log).</summary>
        public static string LastError { get; private set; } = "";

        public static bool TryRead(string dir, out string token, out int port)
        {
            token = "";
            port = 0;
            try
            {
                var path = Path.Combine(dir, "pairing.json");
                if (!File.Exists(path))
                {
                    LastError = "no file at " + path;
                    return false;
                }
                using var doc = JsonDocument.Parse(File.ReadAllText(path));
                var root = doc.RootElement;
                if (!root.TryGetProperty("token", out var t) || !root.TryGetProperty("port", out var p)) return false;
                token = t.GetString() ?? "";
                port = p.GetInt32();
                LastError = token.Length > 0 && port > 0 ? "" : "empty token or port in " + path;
                return token.Length > 0 && port > 0;
            }
            catch (Exception ex)
            {
                LastError = ex.GetType().Name + ": " + ex.Message;
                token = "";
                port = 0;
                return false;
            }
        }
    }
}
