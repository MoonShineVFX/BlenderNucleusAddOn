# Nucleus Connector
**測試版本**: Blender 5.1

透過Nucleus Connector，Blender可直接讀取/編輯/儲存位於Nucleus Server上的檔案。無須手動下載再上傳檔案至Server，同時確保Blender與Omniverse的貼圖、材質保持一致。

架構上是以embed python3.11跑Omniverse Blender Branch Binary library(非開源，Precompiled，可與Nucleus Server溝通)。Blender5.1的python3.13再與embed python3.11傳遞指令。


## 安裝
- 將整個資料夾"blender_nucleus_addon"放置於Blender默認的Add On資料夾下 (ex: C:\Users\MyUserName\AppData\Roaming\Blender Foundation\Blender\5.1\scripts\addons, 替換MyUserName)
- 在Edit > Preferences > Add-ons中，勾選並啟用Add On。啟用後會在右側工具欄位看到Omniverse選項

<img width="1118" height="546" alt="AddOn" src="https://github.com/user-attachments/assets/d8374852-ac02-49b0-8a48-909ccb47470d" />



## 使用
- **Connections (1)**: 點選加號新增Nucleus Server路徑 (ex: omniverse://nucleus.moonshine.tw/)
- **Bookmarks (2)** : 儲存Blender檔後，可自動儲存Nucleus Server位址
- **Directory (3)** : 瀏覽Nucleus Server上的資料夾
- **Files (4)** : 選取想要開啟的USD檔
- **Open (5)** : 從Server上拉取檔案至Blender
- **Save (6)** : 將修改後的USD推回Server
- **Save As (7)** : 將USD檔另存至Server的其他位址
- **Checkpoint (8)(optonal)** : 將檔案推回server時，提交修改的comment

<img width="378" height="721" alt="Panel" src="https://github.com/user-attachments/assets/a35c0d81-9c81-4d3e-ac70-fb6a905c3837" />

## 材質轉換
> 在Omniverse中，除Glass等特殊材質外，盡量使用***USD Preview Surface***，以確保材質在Blender與Omniverse之間能夠正確轉換
- 在Omniverse中，若使用omniPBR(MDL shader language)，輸出後Blender會無法正常讀取
- 在Omniverse中，若使用***USD Preview Surface***或***USD Preview Surface Texture***，Blender可以正常讀取
- 在Blender中，輸出USD時會默認使用***USD Preview Surface***，Omniverse能夠正常讀取
<img width="960" height="525" alt="materials-usdpreviewsurface-graph" src="https://github.com/user-attachments/assets/c9d08b1c-def3-4298-bbf8-fc35fd635f55" />


## Tips
- 一次只能開啟一個主要的USD檔案(與Omniverse設計相同)。載入另一個USD檔時，Add On會提示是否儲存/丟棄變更，並重新載入新檔案
- 不建議直接編輯檔案大小很大的場景，網路傳輸會很久。以修改檔案大小較小的USD檔為主
- 目前的機制是按下Open後，所有的貼圖跟模型都會被存到local cache，關閉Blender或開啟新USD後，Cache會被釋放
- 有修改的貼圖，Save時才會上傳至Server，未修改的貼圖不會重複上傳
- 在已存在的USD檔中新增貼圖，Save時該貼圖也會被上傳至Server
- Login機制由Nucleus server處理，與Add On無關
