// 用 Editor API 生成主场景 —— 免去手写 .unity（YAML）容易出错的问题。
//
// 菜单：RPGBar / 生成主场景
// 命令行：
//   Unity.exe -batchmode -quit -projectPath <项目路径> \
//     -executeMethod RPGBar.EditorTools.RPGBarSceneBuilder.BuildMainScene -logFile <日志>
using System.IO;
using UnityEditor;
using UnityEditor.SceneManagement;
using UnityEngine;

namespace RPGBar.EditorTools
{
    public static class RPGBarSceneBuilder
    {
        public const string SceneDir = "Assets/Scenes";
        public const string ScenePath = SceneDir + "/Main.unity";

        [MenuItem("RPGBar/生成主场景")]
        public static void BuildMainScene()
        {
            if (!Directory.Exists(SceneDir)) Directory.CreateDirectory(SceneDir);

            var scene = EditorSceneManager.NewScene(NewSceneSetup.EmptyScene, NewSceneMode.Single);

            // 相机：UI 用的是 ScreenSpaceOverlay（不依赖相机），但保留一个填充底色的相机
            // 可以让 Game 视图不出现 "No cameras rendering" 警告。
            var camGo = new GameObject("Main Camera");
            camGo.tag = "MainCamera";
            var cam = camGo.AddComponent<Camera>();
            cam.clearFlags = CameraClearFlags.SolidColor;
            cam.backgroundColor = new Color(0.078f, 0.086f, 0.110f, 1f);
            cam.orthographic = true;
            camGo.AddComponent<AudioListener>();

            // 唯一需要摆进场景的业务对象：其余（Canvas / UI / EventSystem）运行时自动创建
            var root = new GameObject("RPGBar");
            root.AddComponent<GameBootstrap>();

            bool saved = EditorSceneManager.SaveScene(scene, ScenePath);

            // 注意顺序：先改 EditorBuildSettings，再 SaveAssets()。
            // 反过来的话这次改动只留在内存里，编辑器一退出就丢了（磁盘上仍是旧场景）。
            EditorBuildSettings.scenes = new[] { new EditorBuildSettingsScene(ScenePath, true) };
            AssetDatabase.SaveAssets();
            AssetDatabase.Refresh();

            if (saved) Debug.Log($"[RPGBar] 主场景已生成：{ScenePath}");
            else Debug.LogError($"[RPGBar] 主场景保存失败：{ScenePath}");
        }

        /// <summary>保证 Build Settings 里只有主场景（第一个即启动场景）。</summary>
        public static void EnsureBuildSettings()
        {
            var scenes = EditorBuildSettings.scenes;
            bool ok = scenes.Length == 1 && scenes[0].path == ScenePath && scenes[0].enabled;

            if (!ok)
            {
                EditorBuildSettings.scenes = new[] { new EditorBuildSettingsScene(ScenePath, true) };
                Debug.Log($"[RPGBar] Build Settings 已指向 {ScenePath}");
            }

            // 无论改没改都落盘：EditorBuildSettings.scenes 赋值只改内存，
            // 不 SaveAssets() 的话编辑器一关就回滚成磁盘上的旧值。
            AssetDatabase.SaveAssets();
        }
    }

    /// <summary>
    /// 打开工程时自动补上主场景（若尚未生成），并把 Build Settings 校正到主场景。
    /// 有了它，第一次打开工程就会自动建好场景，不需要手动点菜单。
    /// </summary>
    [InitializeOnLoad]
    public static class AutoSceneSetup
    {
        static AutoSceneSetup()
        {
            EditorApplication.delayCall += () =>
            {
                if (!File.Exists(RPGBarSceneBuilder.ScenePath))
                {
                    RPGBarSceneBuilder.BuildMainScene();
                    return;
                }
                RPGBarSceneBuilder.EnsureBuildSettings();
            };
        }
    }
}
