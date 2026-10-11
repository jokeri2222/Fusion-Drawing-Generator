"""Built-in help (searchable topics), keyboard shortcuts and the support page."""
import os
import webbrowser

from PyQt5 import QtCore, QtGui, QtWidgets

from . import messages
from .frame import FramedDialog

from . import theme

# Donation page; the QR code next to it is assets/donate_qr.svg.
# While DONATE_URL is empty the Support dialog shows no donate button.
DONATE_URL = 'https://www.paypal.com/donate/?hosted_button_id=DUXNZVDCDAQ8S'

TOPICS = [
    ('Fusion export script', '''
<h2>Fusion export script</h2>
<p>The export script runs <i>inside Fusion 360</i> and writes the <code>exports</code> folder this program reads:
one STEP file per visible body, an archive of the design and <code>project_data.json</code> (parameters, threads,
holes and sketch dimensions).</p>
<h3>Where it is</h3>
<p>It ships with this program as a ready-to-add Fusion script folder: <code>scripts/Export Project For Drawings</code>
(the script and its manifest). <i>Fusion Script &gt; Show export script folder</i> opens it; <i>Copy script path</i>
copies the location.</p>
<h3>Install it in Fusion (once)</h3>
<ol>
<li>In Fusion 360 open <i>Utilities &gt; Add-Ins &gt; Scripts and Add-Ins</i> (or press <b>Shift+S</b>).</li>
<li>On the <i>Scripts</i> tab click the green <b>+</b> next to <i>My Scripts</i> and choose <i>Add existing script
or add-in to My Scripts</i>, then pick the <code>Export Project For Drawings</code> folder.</li>
</ol>
<p>Alternatively copy that folder into Fusion's own scripts folder (<i>Fusion Script &gt; Open Fusion's scripts
folder</i>); it then shows up in the list by itself.</p>
<h3>Run it</h3>
<ol>
<li>Open the design, select <i>Export Project For Drawings</i> in the list and press <b>Run</b>.</li>
<li>Choose where to save. Only bodies that are visible when the script starts are exported; cancelling deletes
what the run created.</li>
<li>Back here, pick the folder that <i>contains</i> <code>exports</code> as the project folder.</li>
</ol>
'''),
    ('Quick start', '''
<h2>Quick start</h2>
<ol>
<li><b>Export from Fusion 360</b> with the export script. It writes a folder named <code>exports</code> holding
<code>project_data.json</code> and one <code>.step</code> file per body.</li>
<li><b>Pick the project folder</b> - the folder that <i>contains</i> <code>exports</code> (<i>File &gt; Open project
folder</i>, the Browse button, or drop the folder on the window). The status line under it counts the STEP files and
how many bodies get a drawing.</li>
<li><b>Set tolerances</b> (optional) and press <i>Save tolerance settings</i>. Drawings only carry &plusmn; figures
once settings have been saved for the project.</li>
<li><b>Check the project settings</b> - which bodies are used, their order and any hotter or colder bodies.</li>
<li>Press <b>Generate drawings</b>. A progress bar shows the stage, and you can cancel at any time. When it finishes,
use <i>Open output folder</i>.</li>
</ol>
<p>Use <b>Assembly instructions (PDF)...</b> for the step-by-step build guide.</p>'''),

    ('Project folder', '''
<h2>The project folder</h2>
<p>Select the folder that contains <code>exports</code>:</p>
<pre>MyProject/
  exports/
    project_data.json
    Housing.step
    Bearing Lid.step
    ...
  configs/        (created by the program - your saved settings)
  drawings/       (default output)</pre>
<p>If you pick the <code>exports</code> folder itself, it works too. The <b>output folder</b> defaults to
<code>&lt;project&gt;/drawings</code>; type or browse to somewhere else if you prefer.</p>
<p>Every part gets its own folder with its drawing (<code>.pdf</code>) and a STEP copy rotated so its features line
up with the axes.</p>'''),

    ('Tolerances and thermal expansion', '''
<h2>Tolerances and thermal expansion</h2>
<p>The &plusmn; figure written on each dimension combines two things:</p>
<ul>
<li><b>Machining margin</b> - the preset's percentage of the dimension. It is kept between the <i>smallest
tolerance</i> (used where parts touch) and the <i>fit band</i> (half of the gap to the nearest part), or the
<i>largest tolerance</i> when no part is close.</li>
<li><b>Thermal movement</b> - how far the dimension changes between the manufacturing temperature and the
operating minimum / maximum, from the part's material.</li>
</ul>
<p>Equal temperatures give no thermal movement; the machining margin still applies.</p>
<h3>Presets</h3>
<p><b>Tight</b>, <b>Standard</b> and <b>Loose</b> are fixed (their numbers are shown greyed out). Choose
<b>Custom</b> to type your own margin, smallest and largest tolerance - those boxes light up when Custom is
selected.</p>
<p>Press <i>Save tolerance settings</i> (Ctrl+S) to store everything for the project. <i>Without tolerances</i>
generates a plain set of drawings and leaves your saved settings alone.</p>'''),

    ('Per-body temperatures', '''
<h2>Per-body temperatures</h2>
<p>Some bodies run hotter or colder than the rest of the assembly - a bracket on a motor, a cold plate. Open
<i>Project &gt; Per-body temperatures...</i>:</p>
<ul>
<li>Left-click a body in the 3D view (or its row) to select it; drag a box to select several. Hold <b>Ctrl</b> or
<b>Shift</b> to add to the selection.</li>
<li>Type a minimum and maximum in any selected row and it is copied to every selected row.</li>
<li><b>Right-drag</b> orbits, <b>middle-drag</b> pans, the <b>wheel</b> zooms. <b>V</b> hides the selected bodies so
you can reach the ones behind (the Hide column does the same).</li>
<li>Bodies written in pink are assembly-only bodies.</li>
</ul>
<p>Leave both boxes empty to use the global operating range.</p>'''),

    ('Body inclusion', '''
<h2>Body inclusion</h2>
<p>Every body is in one of three states:</p>
<ul>
<li><b>Drawing</b> - gets its own dimensioned drawing and appears in the assembly instructions.</li>
<li><b>Assembly only</b> - no drawing of its own, but still shown in the assembly instructions (screws, bearings,
purchased parts).</li>
<li><b>Ignore</b> - left out of everything.</li>
</ul>
<p>The tree mirrors the assembly; choose a row and press a state button.</p>'''),

    ('Assembly order', '''
<h2>Assembly order editor</h2>
<p>The program proposes a build order by analysing which parts touch and support each other. The first analysis can
take a few minutes; results are cached.</p>
<ul>
<li>The list reads top to bottom as the build order. <b>Drag</b> a block onto a container to put it inside, or
between blocks to reorder.</li>
<li><b>Group as assembly</b> wraps the selected blocks into a sub-assembly that is built on its own pages and then
installed as one block. Assemblies nest to any depth.</li>
<li><b>Group as set</b> (dashed, orange) is not an assembly: it gets no pages of its own - its parts are placed
together and named by the set (for example "Bearings").</li>
<li><b>Show fasteners</b> reveals screws and bolts. Their steps are added automatically; merge, split or
delete them, or press <i>Auto-place fasteners</i>.</li>
<li><b>Suggest baseline</b> asks for a fresh proposal; <b>Reset to auto</b> removes your manual order.</li>
<li><b>Ctrl+Z</b> undoes, <b>Delete</b> removes the selected fastener step.</li>
</ul>
<p>The <b>Warnings</b> panel flags problems such as a first part that is not on the ground, or a part that is
placed but not fastened.</p>'''),

    ('Drawing settings', '''
<h2>Drawing settings</h2>
<p>Thresholds that decide which extra dimensions and diameter callouts a drawing gets beyond the recognised holes
and fillets. They are saved per project. The defaults are neutral: nothing is filtered or merged; raise a value to
thin out a crowded view. Length thresholds are shown in your chosen length unit.</p>
<p><i>Reset to defaults</i> restores the neutral values.</p>'''),

    ('Output files', '''
<h2>Output files</h2>
<ul>
<li><code>&lt;output&gt;/&lt;part&gt;/&lt;part&gt;.pdf</code> - the part's drawing: views, ordinate and chain
dimensions, diameter callouts with a legend, a title block (date, projection, units, scale).</li>
<li><code>&lt;output&gt;/&lt;part&gt;/&lt;part&gt;.step</code> - the part rotated to its drawing orientation.</li>
<li>An assembly PDF with every part in place, and <code>assembly_instructions.pdf</code> when requested.</li>
</ul>
<p>Close a PDF in your viewer before regenerating it - Windows will not let the program overwrite a file that is
open.</p>'''),

    ('Units', '''
<h2>Units</h2>
<p>Open <i>Settings &gt; Units (quick pick) or Settings &gt; Preferences</i> to choose length (mm, cm, m, km, &micro;m, nm, in, ft, yd, mi, mil, dm,
nautical mile, &Aring;), angle (degree, radian, gradian, arcminute, arcsecond, milliradian, turn) and temperature
(Celsius, Fahrenheit, Kelvin, Rankine).</p>
<p>The length unit changes the tolerance boxes, drawing-setting thresholds and every dimension printed on the
drawings, including the UNITS field of the title block. Nothing is recalculated - values are only converted for
display. Angle units are stored for future angle dimensions.</p>'''),

    ('Themes', '''
<h2>Themes</h2>
<p>The <b>Themes</b> menu offers Match system (default - it follows Windows live), Light, Dark and a set of
presets. <i>Customize theme...</i> opens the editor:</p>
<ul>
<li>Colours for surfaces, accent, status, and the text colour of each kind of element (titles, headings, hints,
buttons, inputs, log, menus, status bar).</li>
<li>Interface and monospace fonts, text sizes and the corner radius.</li>
<li>Import and export a theme as a <code>.json</code> file to share it.</li>
</ul>
<p>Changes preview live; Cancel restores the previous theme.</p>'''),

    ('Keyboard shortcuts', '''
<h2>Keyboard shortcuts</h2>
<table cellpadding="5">
<tr><td><b>Ctrl+O</b></td><td>Open project folder</td></tr>
<tr><td><b>Ctrl+S</b></td><td>Save tolerance settings</td></tr>
<tr><td><b>Ctrl+G</b></td><td>Generate drawings</td></tr>
<tr><td><b>Ctrl+,</b></td><td>Settings</td></tr>
<tr><td><b>F1</b></td><td>This help</td></tr>
<tr><td><b>Ctrl+Q</b></td><td>Quit</td></tr>
<tr><td><b>Ctrl+Z / Delete</b></td><td>Assembly order editor: undo / delete fastener step</td></tr>
<tr><td><b>V</b></td><td>3D picker: hide or show the selected bodies</td></tr>
</table>'''),

    ('Troubleshooting', '''
<h2>Troubleshooting</h2>
<h3>"No exports folder with project_data.json found"</h3>
<p>Pick the folder that contains <code>exports</code>. Re-run the Fusion export script if the folder is missing.</p>
<h3>Generation fails with "Permission denied"</h3>
<p>A PDF in the output folder is open in another program. Close it and run again.</p>
<h3>The first assembly analysis is slow</h3>
<p>It examines contacts between all bodies. The result is cached, so later runs are quick.</p>
<h3>The 3D view stays empty</h3>
<p>The picker needs VTK (<code>pip install vtk</code>). Large projects take a few seconds to load; a progress
dialog is shown meanwhile.</p>
<h3>A dimension label sits away from its dimension</h3>
<p>When a chain of small dimensions is too crowded, labels move outward and are joined to the dimension by a thin
pointer line ending in a dot, so they never overlap.</p>
<h3>Something unexpected happened</h3>
<p>Errors are shown in a dialog and saved to <code>gui_error.log</code> next to the program.</p>'''),

    ('Glossary', '''
<h2>Glossary</h2>
<dl>
<dt><b>Ordinate / chain dimension</b></dt><dd>Dimensions measured from a common baseline, or one after another
along an edge.</dd>
<dt><b>Fit band</b></dt><dd>The tolerance allowed by the gap to the nearest neighbouring part.</dd>
<dt><b>HLR</b></dt><dd>Hidden-line removal - decides which edges are visible in a view and which are drawn
dashed.</dd>
<dt><b>Assembly-only</b></dt><dd>A body that appears in the build guide but gets no drawing.</dd>
<dt><b>Set</b></dt><dd>A named group of parts placed together that is not a sub-assembly.</dd>
</dl>'''),
]


class HelpDialog(FramedDialog):
    def __init__(self, parent, topic=None):
        super().__init__(parent)
        self.setWindowTitle('Help')
        self.resize(900, 640)
        layout = QtWidgets.QHBoxLayout(self.body)
        left = QtWidgets.QVBoxLayout()
        self.search = QtWidgets.QLineEdit()
        self.search.setPlaceholderText('Search help...')
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._filter)
        left.addWidget(self.search)
        self.list = QtWidgets.QListWidget()
        self.list.setFixedWidth(210)
        left.addWidget(self.list, 1)
        layout.addLayout(left)
        self.view = QtWidgets.QTextBrowser()
        self.view.setOpenExternalLinks(True)
        layout.addWidget(self.view, 1)
        self.list.currentRowChanged.connect(self._show)
        self._visible = []
        self._filter('')
        if topic:
            for i, idx in enumerate(self._visible):
                if TOPICS[idx][0] == topic:
                    self.list.setCurrentRow(i)

    def _filter(self, text):
        text = text.strip().lower()
        self.list.blockSignals(True)
        self.list.clear()
        self._visible = []
        for i, (title, html) in enumerate(TOPICS):
            if not text or text in title.lower() or text in QtGui.QTextDocumentFragment.fromHtml(html).toPlainText().lower():
                self._visible.append(i)
                self.list.addItem(title)
        self.list.blockSignals(False)
        if self._visible:
            self.list.setCurrentRow(0)
            self._show(0)
        else:
            self.view.setHtml('<p>No topic matches that search.</p>')

    def _show(self, row):
        if row < 0 or row >= len(self._visible):
            return
        c = theme.colors()
        doc = self.view.document()
        doc.setDefaultStyleSheet(
            f"body {{ color: {c['text']}; }} h2 {{ color: {c['heading_text']}; }} "
            f"h3 {{ color: {c['heading_text']}; }} a {{ color: {c['accent']}; }} "
            f"code, pre {{ background: {c['surface_alt']}; color: {c['text']}; font-family: Consolas; }}")
        self.view.setHtml(TOPICS[self._visible[row]][1])


class SupportDialog(FramedDialog):
    def __init__(self, parent):
        super().__init__(parent, resizable=False)
        self.setWindowTitle('Support the project')
        self.setMinimumWidth(460)
        lay = QtWidgets.QVBoxLayout(self.body)
        lay.setSpacing(12)
        title = QtWidgets.QLabel('Support Fusion Drawing Generator')
        title.setObjectName('cardTitle')
        lay.addWidget(title)
        text = QtWidgets.QLabel(
            'This tool is built and maintained in spare time. If it saves you hours of drafting, a donation helps '
            'keep it improving - better dimensioning, more checks and new export options. It is never required, '
            'and every feature stays available either way.\n\nThank you!')
        text.setWordWrap(True)
        lay.addWidget(text)
        qr_path = __import__('paths').resource('assets', 'donate_qr.svg')
        if os.path.exists(qr_path):
            from PyQt5 import QtSvg
            dpr = self.devicePixelRatioF()
            side = 200
            pix = QtGui.QPixmap(int(side * dpr), int(side * dpr))
            pix.setDevicePixelRatio(dpr)
            pix.fill(QtCore.Qt.transparent)
            painter = QtGui.QPainter(pix)
            QtSvg.QSvgRenderer(qr_path).render(painter, QtCore.QRectF(0, 0, side, side))
            painter.end()
            qr = QtWidgets.QLabel()
            qr.setPixmap(pix)
            qr.setAlignment(QtCore.Qt.AlignCenter)
            qr.setStyleSheet('background: white; border-radius: 8px; padding: 10px;')
            qr.setToolTip('Scan with your phone to donate via PayPal')
            lay.addWidget(qr, 0, QtCore.Qt.AlignCenter)
            scan = QtWidgets.QLabel('Scan with your phone, or use the button.')
            scan.setProperty('muted', True)
            scan.setAlignment(QtCore.Qt.AlignCenter)
            lay.addWidget(scan)
        row = QtWidgets.QHBoxLayout()
        row.addStretch(1)
        close = QtWidgets.QPushButton('Close')
        close.clicked.connect(self.accept)
        donate = QtWidgets.QPushButton('Donate...')
        donate.setObjectName('primary')
        donate.setEnabled(bool(DONATE_URL))
        donate.setToolTip(DONATE_URL or 'No donation page has been set up yet.')
        donate.clicked.connect(lambda: webbrowser.open(DONATE_URL))
        row.addWidget(close)
        row.addWidget(donate)
        lay.addLayout(row)
        self.adjustSize()
