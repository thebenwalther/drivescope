import QtQuick
import QtQuick.Controls
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Commons as Commons
import qs.Ui

// A bar icon and popup showing each external drive's live speed and whether
// it's safe to unplug. All the numbers come from bin/drivescope-watch, which
// shares its logic with the DriveScope app. Nothing here can change a drive.
Panel {
  id: root
  moduleName: "io.github.thebenwalther.drivescope"
  ipcTarget: root.moduleName

  property var manifest: null
  property var snapshot: null
  property var history: ({})
  property var driveIds: []
  // Set when the watcher goes quiet. Its last answer may no longer be true, so
  // nothing it said is shown, least of all "Safe to unplug".
  property bool stale: false

  readonly property int historyPoints: 60
  readonly property string sourceDir: {
    if (manifest && manifest.__sourceDir) return manifest.__sourceDir
    return decodeURIComponent(String(Qt.resolvedUrl("..")).replace(/^file:\/\//, "")).replace(/\/$/, "")
  }
  readonly property bool ready: !stale && !!snapshot && snapshot.ready
  readonly property string state: ready ? snapshot.state : "none"
  readonly property string activity: ready && root.setting("showSpeed", true) !== false ? snapshot.activity : ""
  readonly property bool appAvailable: !!snapshot && snapshot.appAvailable === true
  readonly property string errorText: !!snapshot && !stale ? String(snapshot.error || "") : ""
  readonly property color fg: bar ? bar.foreground : Commons.Color.foreground
  readonly property color dim: Qt.darker(fg, 1.5)
  readonly property string fontFamily: bar ? bar.fontFamily : Style.font.family
  // The logo is never recolored, so pick the version made for this background:
  // light text means a dark bar. Same rule as the app.
  readonly property url logo: Util.fileUrl(root.sourceDir + "/data/icons/hicolor/128x128/apps/dev.drivescope.DriveScope"
    + (root.barForeground.hslLightness >= 0.5 ? "" : "-light") + ".png")

  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  function driveById(id) {
    var drives = root.snapshot ? root.snapshot.drives : []
    for (var i = 0; i < drives.length; i++) {
      if (drives[i].id === id) return drives[i]
    }
    return null
  }

  function handleLine(line) {
    var next
    try { next = JSON.parse(line) } catch (e) { return }
    if (!next || next.version !== 1 || !Array.isArray(next.drives)) return
    watchdog.restart()
    root.stale = false

    var nextHistory = {}
    var ids = []
    for (var i = 0; i < next.drives.length; i++) {
      var drive = next.drives[i]
      var past = root.history[drive.id] || { read: [], write: [] }
      nextHistory[drive.id] = {
        read: past.read.concat([drive.readBps]).slice(-root.historyPoints),
        write: past.write.concat([drive.writeBps]).slice(-root.historyPoints)
      }
      ids.push(drive.id)
    }
    root.history = nextHistory
    root.snapshot = next
    // Only a change in the set of drives rebuilds the rows, so they don't
    // flicker or lose their scroll position on every update.
    if (ids.join("/") !== root.driveIds.join("/")) root.driveIds = ids
  }

  function start() {
    if (!watcher.running) watcher.running = true
    watchdog.restart()
  }

  function openApp() {
    if (!root.appAvailable) return
    Quickshell.execDetached([root.sourceDir + "/bin/drivescope"])
    root.close()
  }

  function stateGlyph(state) {
    if (state === "safe") return "󰄬"
    if (state === "busy") return "󰔟"
    return "󰀦"
  }

  function stateColor(state) {
    if (state === "safe") return root.fg
    if (state === "busy") return Commons.Color.accent
    return Commons.Color.urgent
  }

  Component.onCompleted: Qt.callLater(root.start)

  Process {
    id: watcher
    command: [root.sourceDir + "/bin/drivescope-watch"]
    stdout: SplitParser {
      onRead: function(line) { root.handleLine(line) }
    }
    onExited: restartTimer.restart()
  }

  Timer {
    id: restartTimer
    interval: 5000
    onTriggered: root.start()
  }

  // A hung or crashed watcher stops sending lines. Stop trusting it, and restart it.
  Timer {
    id: watchdog
    interval: 8000
    onTriggered: {
      root.stale = true
      if (watcher.running) watcher.running = false
      else restartTimer.restart()
    }
  }

  WidgetButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    labelVisible: false
    hasVisualContent: true
    dimmed: root.driveIds.length === 0 || root.stale
    active: root.state === "busy"
    activeColor: Commons.Color.accent
    tooltipText: root.opened ? "" : (root.stale ? "DriveScope isn't updating" : (root.snapshot ? root.snapshot.tooltip : "DriveScope"))
    fixedWidth: vertical ? -1 : Math.ceil(barContent.implicitWidth + button.scaledHorizontalMargin * 2)
    fixedHeight: vertical ? Style.bar.iconSlot : -1
    onPressed: function(code) { root.toggle() }

    Row {
      id: barContent
      anchors.centerIn: parent
      spacing: Style.space(5)

      Logo {
        size: Style.bar.iconCanvas
        anchors.verticalCenter: parent.verticalCenter
      }

      Text {
        textFormat: Text.PlainText
        visible: !button.vertical && root.activity !== ""
        text: root.activity
        color: button.active ? button.activeColor : button.foreground
        font.family: button.fontFamily
        font.pixelSize: Style.font.caption
        font.bold: true
        anchors.verticalCenter: parent.verticalCenter
      }
    }
  }

  KeyboardPanel {
    id: panel
    anchorItem: button
    owner: root
    bar: root.bar
    open: root.opened
    focusTarget: keyCatcher
    contentWidth: panel.fittedContentWidth(Style.space(400))
    contentHeight: panel.fittedContentHeight(column.implicitHeight)

    PanelKeyCatcher {
      id: keyCatcher
      anchors.fill: parent

      onActivateRequested: root.openApp()
      onCloseRequested: root.close()
      onTabRequested: function(direction) { root.switchPanel(direction) }

      Column {
        id: column
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.top: parent.top
        spacing: Style.space(12)

        Item {
          width: parent.width
          implicitHeight: Math.max(heroIcon.height, heroLabels.implicitHeight)

          Logo {
            id: heroIcon
            size: Style.font.display
            anchors.left: parent.left
            anchors.verticalCenter: parent.verticalCenter
          }

          Column {
            id: heroLabels
            anchors.left: heroIcon.right
            anchors.leftMargin: Style.space(14)
            anchors.right: parent.right
            anchors.verticalCenter: parent.verticalCenter
            spacing: Style.space(2)

            Text {
              textFormat: Text.PlainText
              text: "DriveScope"
              color: root.fg
              font.family: root.fontFamily
              font.pixelSize: Style.font.title
              font.bold: true
            }

            Text {
              textFormat: Text.PlainText
              text: {
                if (root.stale) return "NOT UPDATING"
                if (!root.ready) return "LOOKING FOR DRIVES…"
                var count = root.driveIds.length
                return count === 1 ? "1 EXTERNAL DRIVE" : count + " EXTERNAL DRIVES"
              }
              color: Qt.darker(root.fg, 1.4)
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
              font.bold: true
              font.letterSpacing: 1.2
            }
          }
        }

        PanelSeparator { foreground: root.fg }

        ListView {
          width: parent.width
          height: Math.min(contentHeight, Style.space(480))
          visible: root.ready && root.driveIds.length > 0
          spacing: Style.space(14)
          clip: true
          boundsBehavior: Flickable.StopAtBounds
          interactive: contentHeight > height
          ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }
          model: root.driveIds

          delegate: DriveRow {
            required property var modelData
            width: ListView.view.width
            driveId: modelData
          }
        }

        Text {
          textFormat: Text.PlainText
          visible: text !== ""
          width: parent.width
          wrapMode: Text.WordWrap
          text: {
            if (root.stale) return "Lost contact with the drive monitor, so it can't say whether a drive is safe to unplug. Trying again."
            if (root.errorText !== "") return root.errorText
            if (root.ready && root.driveIds.length === 0) return "No external drive connected."
            return ""
          }
          color: root.stale || root.errorText !== "" ? root.fg : root.dim
          font.family: root.fontFamily
          font.pixelSize: Style.font.bodySmall
        }

        PanelSeparator { foreground: root.fg }

        Item {
          width: parent.width
          implicitHeight: Math.max(hint.implicitHeight, openButton.implicitHeight)

          Text {
            id: hint
            textFormat: Text.PlainText
            text: root.appAvailable ? "↵ benchmarks and health" : "Benchmarks need the DriveScope app. See the README."
            color: root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
            elide: Text.ElideRight
            anchors.left: parent.left
            anchors.right: openButton.visible ? openButton.left : parent.right
            anchors.rightMargin: Style.space(8)
            anchors.verticalCenter: parent.verticalCenter
          }

          Button {
            id: openButton
            visible: root.appAvailable
            text: "Open DriveScope"
            fontSize: Style.font.bodySmall
            foreground: root.fg
            fontFamily: root.fontFamily
            bordered: true
            anchors.right: parent.right
            anchors.verticalCenter: parent.verticalCenter
            onClicked: root.openApp()
          }
        }
      }
    }
  }

  component Logo: Image {
    property real size: 0
    width: size
    height: size
    sourceSize.width: size * 2
    sourceSize.height: size * 2
    source: root.logo
    fillMode: Image.PreserveAspectFit
    smooth: true
    mipmap: true
  }

  component Speed: Row {
    id: speed
    property color keyColor: root.fg
    property string text: ""
    spacing: Style.space(6)

    Rectangle {
      width: Style.space(10)
      height: Math.max(2, Style.space(2))
      radius: height / 2
      color: speed.keyColor
      anchors.verticalCenter: parent.verticalCenter
    }

    Text {
      textFormat: Text.PlainText
      text: speed.text
      color: root.fg
      font.family: root.fontFamily
      font.pixelSize: Style.font.bodySmall
      font.bold: true
    }
  }

  component DriveRow: Column {
    id: row
    required property string driveId
    readonly property var drive: root.driveById(driveId)
    readonly property var past: root.history[driveId] || { read: [], write: [] }
    spacing: Style.space(5)

    Item {
      width: parent.width
      implicitHeight: Math.max(nameText.implicitHeight, sizeText.implicitHeight)

      Text {
        id: nameText
        textFormat: Text.PlainText
        text: row.drive ? row.drive.name : ""
        color: root.fg
        font.family: root.fontFamily
        font.pixelSize: Style.font.body
        font.bold: true
        elide: Text.ElideRight
        anchors.left: parent.left
        anchors.right: sizeText.left
        anchors.rightMargin: Style.space(8)
      }

      Text {
        id: sizeText
        textFormat: Text.PlainText
        text: row.drive ? row.drive.size : ""
        color: root.dim
        font.family: root.fontFamily
        font.pixelSize: Style.font.caption
        anchors.right: parent.right
        anchors.verticalCenter: parent.verticalCenter
      }
    }

    Text {
      textFormat: Text.PlainText
      width: parent.width
      text: row.drive ? row.drive.volumes + " · " + row.drive.link : ""
      color: root.dim
      font.family: root.fontFamily
      font.pixelSize: Style.font.caption
      elide: Text.ElideRight
    }

    Item {
      width: parent.width
      implicitHeight: speeds.implicitHeight

      Row {
        id: speeds
        anchors.left: parent.left
        spacing: Style.space(14)

        Speed {
          keyColor: Commons.Color.accent
          text: row.drive ? "Read " + row.drive.read : ""
        }

        Speed {
          keyColor: root.fg
          text: row.drive ? "Write " + row.drive.write : ""
        }
      }

      Text {
        textFormat: Text.PlainText
        text: row.drive ? "Busy " + row.drive.busy : ""
        color: root.dim
        font.family: root.fontFamily
        font.pixelSize: Style.font.caption
        anchors.right: parent.right
        anchors.verticalCenter: parent.verticalCenter
      }
    }

    Canvas {
      id: chart
      width: parent.width
      height: Style.space(34)
      readonly property var reads: row.past.read
      readonly property var writes: row.past.write
      onReadsChanged: if (root.opened) requestPaint()
      onWidthChanged: requestPaint()

      function line(ctx, values, ceiling, color) {
        // An idle direction draws nothing, leaving only the baseline.
        if (values.length < 2 || Math.max.apply(null, values) <= 0) return
        var step = width / (root.historyPoints - 1)
        var offset = root.historyPoints - values.length
        ctx.beginPath()
        for (var i = 0; i < values.length; i++) {
          var x = (i + offset) * step
          var y = height - 1 - (values[i] / ceiling) * (height - 2)
          if (i === 0) ctx.moveTo(x, y)
          else ctx.lineTo(x, y)
        }
        ctx.lineWidth = 1.5
        ctx.strokeStyle = color
        ctx.stroke()
      }

      onPaint: {
        var ctx = getContext("2d")
        ctx.reset()
        // A floor keeps background noise from looking like a full-height spike.
        var ceiling = Math.max(1000000, Math.max.apply(null, reads.concat(writes)))
        ctx.beginPath()
        ctx.moveTo(0, height - 0.5)
        ctx.lineTo(width, height - 0.5)
        ctx.lineWidth = 1
        ctx.strokeStyle = Qt.rgba(root.fg.r, root.fg.g, root.fg.b, 0.2)
        ctx.stroke()
        line(ctx, writes, ceiling, root.fg)
        line(ctx, reads, ceiling, Commons.Color.accent)
      }
    }

    Row {
      width: parent.width
      spacing: Style.space(7)

      Text {
        id: stateIcon
        textFormat: Text.PlainText
        text: row.drive ? root.stateGlyph(row.drive.state) : ""
        color: row.drive ? root.stateColor(row.drive.state) : root.fg
        font.family: root.fontFamily
        font.pixelSize: Style.font.body
      }

      Column {
        width: parent.width - stateIcon.width - parent.spacing
        spacing: Style.space(1)

        Text {
          textFormat: Text.PlainText
          text: row.drive ? row.drive.title : ""
          color: root.fg
          font.family: root.fontFamily
          font.pixelSize: Style.font.bodySmall
          font.bold: true
        }

        Text {
          textFormat: Text.PlainText
          width: parent.width
          wrapMode: Text.WordWrap
          text: row.drive ? row.drive.detail : ""
          color: root.dim
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption
        }
      }
    }

    Repeater {
      model: row.drive ? row.drive.warnings : []

      Row {
        id: warning
        required property string modelData
        width: row.width
        spacing: Style.space(7)

        Text {
          id: warningIcon
          textFormat: Text.PlainText
          text: "󰀦"
          color: Commons.Color.urgent
          font.family: root.fontFamily
          font.pixelSize: Style.font.body
        }

        Text {
          textFormat: Text.PlainText
          width: parent.width - warningIcon.width - parent.spacing
          wrapMode: Text.WordWrap
          text: warning.modelData
          color: root.fg
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption
        }
      }
    }
  }
}
