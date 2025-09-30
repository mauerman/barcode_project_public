import os
import psycopg2
import psycopg2.extras
from flask import Flask, render_template, request, url_for
import time
from barcode import EAN13
from barcode.writer import ImageWriter
from dotenv import load_dotenv

app = Flask(__name__, template_folder='templates', static_folder='static')

ENABLE_CAMERA = os.getenv("ENABLE_CAMERA", "0") == "1"
BASE_DIR = os.path.dirname(__file__)
load_dotenv(os.path.join(BASE_DIR, 'my_info.env'))

# --------------- Database Connection ---------------

def get_db_connection():
    db_url = os.getenv("DATABASE_URL")
    if db_url:
        conn = psycopg2.connect(db_url)
        return conn

    host = os.getenv("DATABASE_HOST")
    if not host:
        raise RuntimeError(
            "Manglende database-konfiguration. Sæt enten DATABASE_URL "
            "eller DATABASE_HOST/DATABASE_PORT/DATABASE_NAME/DATABASE_USER/DATABASE_PASSWORD i my_info.env."
        )

    conn = psycopg2.connect(
        host=host,
        port=os.getenv("DATABASE_PORT"),
        dbname=os.getenv("DATABASE_NAME"),
        user=os.getenv("DATABASE_USER"),
        password=os.getenv("DATABASE_PASSWORD"),
        sslmode=os.getenv("DB_SSLMODE", "require"),
    )
    return conn


# --------------- Barcode helpers ---------------

def parse_tags(raw: str | None) -> list[str] | None:
    """
    'sodavand, 1.5L, Cola' -> ['sodavand','1.5l','cola']
    Returnerer [] hvis tom streng, None hvis raw er None.
    """
    if raw is None:
        return None
    tags = [t.strip().lower() for t in raw.split(',') if t.strip()]
    # fjern dubletter men bevar rækkefølge
    return list(dict.fromkeys(tags))


def read_barcode_from_camera():
    if not ENABLE_CAMERA:
        raise RuntimeError("Camera is disabled in this environment")

    import cv2
    from zxingcpp import read_barcodes
    """
    Scanner én stregkode via webcam og returnerer teksten (EAN) som str.
    Lukker kamera og vinduer, når en kode er fundet eller 'q' trykkes.
    """
    camera = cv2.VideoCapture(camera_adr)
    if not camera.isOpened():
        print("Fejl: Kunne ikke åbne kamera.")
        return None

    camera.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
    camera.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
    time.sleep(1)

    found_text = None
    while True:
        ret, frame = camera.read()
        if not ret:
            print("Fejl: Kunne ikke læse en ramme.")
            break

        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        barcodes = read_barcodes(gray)

        if barcodes:
            # Tag første fund
            bc = barcodes[0]
            found_text = str(bc.text).strip()
            # Vis en ramme med grøn boks (valgfrit)
            try:
                p1 = (int(bc.position.top_left.x), int(bc.position.top_left.y))
                p2 = (int(bc.position.top_right.x), int(bc.position.top_right.y))
                p3 = (int(bc.position.bottom_right.x), int(bc.position.bottom_right.y))
                p4 = (int(bc.position.bottom_left.x), int(bc.position.bottom_left.y))
                cv2.line(frame, p1, p2, (0, 255, 0), 2)
                cv2.line(frame, p2, p3, (0, 255, 0), 2)
                cv2.line(frame, p3, p4, (0, 255, 0), 2)
                cv2.line(frame, p4, p1, (0, 255, 0), 2)
                cv2.imshow("Barcode Scanner", frame)
                cv2.waitKey(500)  # kort visning
            except Exception:
                pass
            break

        cv2.imshow("Barcode Scanner", frame)
        if cv2.waitKey(1) & 0xFF == ord('q'):
            break

    camera.release()
    try:
        cv2.destroyAllWindows()
    except Exception:
        pass
    return found_text


def save_barcode_simple(ean_number: str) -> str | None:
    """
    Gemmer 'barcode {EAN}.png' i static/barcodes hvis ikke den findes.
    Returnerer relativ sti (fx 'barcodes/barcode 5701234567890.png') eller None.
    """
    clean = str(ean_number).strip().replace(".", "")
    clean = "".join(ch for ch in clean if ch.isdigit())
    if len(clean) < 12:
        return None  # for kort til EAN13

    out_dir = os.path.join(app.static_folder, "barcodes")
    os.makedirs(out_dir, exist_ok=True)

    base = f"barcode {clean}"
    png_path = os.path.join(out_dir, base + ".png")

    if os.path.exists(png_path):
        # findes allerede – spring over (som i dit eksempel)
        print(f"{base} findes allerede – springer over")
        return f"barcodes/{base}.png"

    # python-barcode EAN13 vil have 12 cifre (uden checksum)
    data12 = clean[:12]
    EAN13(data12, writer=ImageWriter()).save(os.path.join(out_dir, base))
    return f"barcodes/{base}.png"

def ensure_dirs():
    os.makedirs(os.path.join(app.static_folder, 'barcodes'), exist_ok=True)
    os.makedirs(os.path.join(app.static_folder, 'product_img'), exist_ok=True)

# --- Interaktiv foto-capture via OpenCV (med klikbar "Tag foto"-knap) ---

_capture_click = {"pressed": False}

def _mouse_cb(event, x, y, flags, param):
    """
    Mouse callback for at registrere klik på 'Tag foto'-knappen.
    param forventes at være en dict med 'btn_rect': (x1,y1,x2,y2).
    """
    if event == cv2.EVENT_LBUTTONDOWN and param and "btn_rect" in param:
        x1, y1, x2, y2 = param["btn_rect"]
        if x1 <= x <= x2 and y1 <= y <= y2:
            _capture_click["pressed"] = True

def capture_photo_interactive_to_static(basename: str, camera_adr: int = 0) -> str | None:
    """
    Viser live kamerabillede i et vindue med en klikbar 'Tag foto'-knap.
    SPACE/ENTER tager også foto. 'q' annullerer.
    Gemmer som JPG i static/product_img/{basename}.jpg
    Returnerer relativ sti 'product_img/xxx.jpg' eller None.
    """
    ensure_dirs()
    cap = cv2.VideoCapture(camera_adr)
    if not cap.isOpened():
        print("Fejl: kunne ikke åbne kamera.")
        return None

    # (valgfrit) lidt opstartsdelay
    time.sleep(0.3)

    window_name = "Produktfoto (klik 'Tag foto' eller tryk SPACE/ENTER, 'q' for at annullere)"
    cv2.namedWindow(window_name)

    # knap-dimensioner (relative)
    btn_w, btn_h = 180, 48
    margin = 20

    # mouse-callback state
    state = {"btn_rect": (0, 0, 0, 0)}
    cv2.setMouseCallback(window_name, _mouse_cb, state)

    captured = None
    while True:
        ok, frame = cap.read()
        if not ok:
            print("Fejl: kunne ikke læse fra kamera.")
            break

        # Beregn knap-position i bunden til højre
        h, w = frame.shape[:2]
        x2 = w - margin
        y2 = h - margin
        x1 = x2 - btn_w
        y1 = y2 - btn_h
        state["btn_rect"] = (x1, y1, x2, y2)

        # Tegn semitransparent knap
        overlay = frame.copy()
        cv2.rectangle(overlay, (x1, y1), (x2, y2), (255, 255, 255), -1)
        alpha = 0.35
        frame = cv2.addWeighted(overlay, alpha, frame, 1 - alpha, 0)

        # Kant + tekst
        cv2.rectangle(frame, (x1, y1), (x2, y2), (230, 230, 230), 2)
        label = "Tag foto"
        (tw, th), _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.8, 2)
        tx = x1 + (btn_w - tw) // 2
        ty = y1 + (btn_h + th) // 2 - 4
        cv2.putText(frame, label, (tx, ty), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (20, 20, 20), 2, cv2.LINE_AA)

        # Instruktioner
        cv2.putText(frame, "SPACE/ENTER: tag foto  |  q: annuller",
                    (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (240, 240, 240), 2, cv2.LINE_AA)

        cv2.imshow(window_name, frame)

        key = cv2.waitKey(1) & 0xFF
        if key in (13, 32):  # ENTER(13) eller SPACE(32)
            captured = frame.copy()
            break
        if key == ord('q'):
            captured = None
            break
        if _capture_click["pressed"]:
            captured = frame.copy()
            _capture_click["pressed"] = False
            break

    cap.release()
    try:
        cv2.destroyWindow(window_name)
    except Exception:
        pass

    if captured is None:
        return None

    rel_path = f"product_img/{basename}.jpg"
    abs_path = os.path.join(app.static_folder, rel_path)

    # Gem som JPG (kan evt. nedskalere/komprimere her hvis ønsket)
    ok = cv2.imwrite(abs_path, captured)
    if not ok:
        print("Fejl: kunne ikke gemme produktfoto.")
        return None

    return rel_path



# --------------- Home / Welcome Page ---------------
@app.route('/')
def index():
    """
    Renders the welcome page with a company logo.
    """
    return render_template('index.html')

# --------------- Search Products ---------------
@app.route('/search', methods=['GET', 'POST'])
def search():
    """
    Søg på:
      - product_id (eksakt), eller
      - tags (komma-separeret). Viser liste ved flere resultater.
    """
    product = None
    image_url = None
    error = None

    if request.method == 'POST':
        product_id = (request.form.get('product_id') or '').strip()
        tags_q     = (request.form.get('tags') or '').strip()

        try:
            if product_id.isdigit():
                # Slå op på ID
                conn = get_db_connection()
                cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
                cur.execute("""
                    SELECT product_id, product_ean, product_name, product_desc,
                           product_image, stock_qty, tags
                    FROM produkter
                    WHERE product_id = %s
                """, (int(product_id),))
                product = cur.fetchone()
                cur.close(); conn.close()

                # Lokal stregkode: PNG først, SVG fallback
                if product and product.get('product_ean'):
                    ean = ''.join(ch for ch in str(product['product_ean']) if ch.isdigit())
                    barcode_dir = os.path.join(app.static_folder, 'barcodes')
                    for fname in (f"barcode {ean}.png", f"barcode {ean}.svg"):
                        if os.path.exists(os.path.join(barcode_dir, fname)):
                            image_url = url_for('static', filename=f'barcodes/{fname}')
                            break

            elif tags_q:
                # Søgning på tags (mindst ét overlap)
                terms = [t.strip().lower() for t in tags_q.split(',') if t.strip()]
                if terms:
                    conn = get_db_connection()
                    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
                    cur.execute("""
                        SELECT product_id, product_ean, product_name, product_desc, stock_qty
                        FROM produkter
                        WHERE tags && %s::text[]
                        ORDER BY product_id
                    """, (terms,))
                    rows = cur.fetchall()
                    cur.close(); conn.close()
                    # Vis liste-skabelonen ved tag-søgning
                    return render_template('products.html', rows=rows, error=None)
                else:
                    error = "Indtast mindst ét gyldigt tag."

            else:
                error = "Indtast enten et product_id eller nogle tags."

        except Exception as e:
            error = f"Databasefejl: {e}"

    return render_template('search.html', product=product, image_url=image_url, error=error)



# --------------- Products: List / Create / Read / Update / Delete ---------------

@app.route('/products/<int:product_id>/qty/add', methods=['POST'])
def products_qty_add(product_id):
    from flask import request, url_for, render_template
    try:
        delta = int(request.form.get('delta', 1))
    except ValueError:
        delta = 0

    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("UPDATE produkter SET stock_qty = stock_qty + %s WHERE product_id = %s",
                    (delta, product_id))
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        return render_template('product_detail.html',
                               error=f'DB-fejl: {e}', product={'product_id': product_id})

    return render_template('redirect.html', target=url_for('products_detail', product_id=product_id))


# sæt antal direkte
@app.route('/products/<int:product_id>/qty/set', methods=['POST'])
def products_qty_set(product_id):
    from flask import request, url_for, render_template
    try:
        new_qty = int(request.form.get('qty', 0))
        if new_qty < 0:
            new_qty = 0
    except ValueError:
        new_qty = 0

    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("UPDATE produkter SET stock_qty = %s WHERE product_id = %s",
                    (new_qty, product_id))
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        return render_template('product_detail.html',
                               error=f'DB-fejl: {e}', product={'product_id': product_id})

    return render_template('redirect.html', target=url_for('products_detail', product_id=product_id))

@app.route('/products/scan-increment', methods=['GET'])
def products_scan_increment():
    if not ENABLE_CAMERA:
        return render_template('redirect.html', target=url_for('products_list'))
    """
    Scanner en stregkode og lægger delta til lageret.
    Hvis produktet ikke findes, redirecter til 'Nyt produkt' med EAN forudfyldt.
    """
    try:
        delta = int(request.args.get('delta', 1))
    except (TypeError, ValueError):
        delta = 1

    ean = read_barcode_from_camera()
    if not ean:
        return render_template('redirect.html', target=url_for('products_list'))

    clean = ''.join(ch for ch in str(ean) if ch.isdigit())
    if len(clean) < 12:
        return render_template('redirect.html', target=url_for('products_list'))

    # Gem 'barcode {EAN}.png' i static/barcodes hvis ikke den findes (simpel helper)
    try:
        save_barcode_simple(clean)
    except Exception:
        pass

    # DB: find/opdater, ellers send til create med EAN udfyldt
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        cur.execute("SELECT product_id FROM produkter WHERE product_ean = %s", (clean,))
        row = cur.fetchone()

        if row:
            pid = row['product_id']
            cur.execute(
                "UPDATE produkter SET stock_qty = stock_qty + %s WHERE product_id = %s",
                (delta, pid)
            )
            conn.commit()
            cur.close(); conn.close()
            return render_template('redirect.html', target=url_for('products_detail', product_id=pid))
        else:
            cur.close(); conn.close()
            return render_template('redirect.html', target=url_for('products_create', ean=clean))
    except Exception:
        return render_template('redirect.html', target=url_for('products_list'))



@app.route('/products/<int:product_id>/photo')
def products_photo_edit(product_id):
    """
    Interaktivt produktfoto for eksisterende produkt. Navngiver efter EAN hvis muligt.
    Opdaterer product_image i DB og returnerer til detaljevisning.
    """
    # Hent EAN
    ean = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT product_ean FROM produkter WHERE product_id = %s", (product_id,))
        row = cur.fetchone()
        cur.close(); conn.close()
        if row: ean = (row.get('product_ean') or '').strip()
    except Exception as e:
        return render_template('product_detail.html',
                               error=f"Databasefejl ved hentning: {e}",
                               product={'product_id': product_id})

    base = f"product_{ean}" if ean else f"product_id_{product_id}"
    img_rel = capture_photo_interactive_to_static(base)
    if not img_rel:
        return render_template('product_detail.html',
                               error="Foto blev annulleret eller mislykkedes.",
                               product={'product_id': product_id})

    # Gem sti i DB
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("""
            UPDATE produkter
               SET product_image = %s
             WHERE product_id   = %s
        """, (img_rel, product_id))
        conn.commit()
        cur.close(); conn.close()
    except Exception as e:
        return render_template('product_detail.html',
                               error=f"Databasefejl ved opdatering: {e}",
                               product={'product_id': product_id})

    return render_template('redirect.html', target=url_for('products_detail', product_id=product_id))


@app.route('/products/new/photo')
def products_photo_new():
    """
    Interaktivt produktfoto ved create. Kræver ?ean=... (bruges til filnavn).
    Returnerer redirect til create-form med ?ean=...&img=...
    """
    ean = (request.args.get('ean') or '').strip()
    if not ean:
        return render_template('product_form.html', mode='create',
                               error="Mangler EAN til foto. Scan eller indtast EAN først.",
                               product=None)

    img_rel = capture_photo_interactive_to_static(f"product_{ean}")
    if not img_rel:
        return render_template('product_form.html', mode='create',
                               error="Foto blev annulleret eller mislykkedes.",
                               product={'product_ean': ean, 'product_name': '', 'product_desc': ''})

    return render_template('redirect.html', target=url_for('products_create', ean=ean, img=img_rel))


@app.route('/products')
def products_list():
    """
    Viser alle produkter i en tabel med links til vis/rediger/slet.
    """
    rows = []
    error = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            SELECT product_id, product_ean, product_name, product_desc, stock_qty
            FROM produkter
            ORDER BY product_id ASC
            """)
        rows = cur.fetchall()
        cur.close()
        conn.close()
    except Exception as e:
        error = f"Databasefejl: {e}"

    return render_template('products.html', rows=rows, error=error)


@app.route('/products/new', methods=['GET', 'POST'])
def products_create():
    """
    Opret/UPSERT produkt. Understøtter ?ean=...&img=...
    Tags kan indtastes komma-separeret; gemmes som TEXT[].
    """
    error = None
    prefilled = {
        'product_ean': (request.args.get('ean') or '').strip(),
        'product_name': '',
        'product_desc': '',
        'product_image': (request.args.get('img') or '').strip(),
        'tags': '',  # vises i form som rå tekst
    }

    if request.method == 'POST':
        product_ean = ''.join(ch for ch in (request.form.get('product_ean') or '').strip() if ch.isdigit())
        product_name = (request.form.get('product_name') or '').strip()
        product_desc = (request.form.get('product_desc') or '').strip()
        product_image = (request.form.get('product_image') or '').strip() or None

        tags_raw = request.form.get('tags')
        tags_list = parse_tags(tags_raw) or []   # tom liste hvis blank

        if not product_ean or not product_name:
            error = "EAN og navn skal udfyldes."
        else:
            try:
                conn = get_db_connection()
                cur = conn.cursor()
                cur.execute("""
                    INSERT INTO produkter (product_ean, product_name, product_desc, product_image, tags)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (product_ean) DO UPDATE
                    SET product_name  = EXCLUDED.product_name,
                        product_desc  = EXCLUDED.product_desc,
                        product_image = COALESCE(EXCLUDED.product_image, produkter.product_image),
                        tags          = COALESCE(NULLIF(EXCLUDED.tags, '{}'::text[]), produkter.tags)
                    RETURNING product_id
                """, (product_ean, product_name, product_desc, product_image, tags_list))
                new_id = cur.fetchone()[0]
                conn.commit()
                cur.close(); conn.close()
                return render_template('redirect.html', target=url_for('products_detail', product_id=new_id))
            except Exception as e:
                error = f"Databasefejl: {e}"

        prefilled = {
            'product_ean': product_ean,
            'product_name': product_name,
            'product_desc': product_desc,
            'product_image': (product_image or ''),
            'tags': (tags_raw or ''),
        }

    return render_template('product_form.html', mode='create', error=error, product=prefilled)




@app.route('/products/new/scan', methods=['GET'])
def products_scan():
    if not ENABLE_CAMERA:
        return render_template('redirect.html', target=url_for('products_list'))    
    ean = read_barcode_from_camera()
    if not ean:
        return render_template('product_form.html', mode='create',
                               error="Ingen stregkode fundet.", product=None)

    clean = ''.join(ch for ch in str(ean) if ch.isdigit())
    if len(clean) < 12:
        return render_template('product_form.html', mode='create',
                               error="Ugyldig EAN (for kort).", product={'product_ean': clean})

    # Gem 'barcode {EAN}.png' i static/barcodes hvis ikke den findes
    save_barcode_simple(clean)

    # Forudfyld create-formularen med EAN
    return render_template('redirect.html', target=url_for('products_create', ean=clean))



@app.route('/products/<int:product_id>')
def products_detail(product_id):
    product = None
    image_url = None
    error = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            SELECT product_id, product_ean, product_name, product_desc, product_image, stock_qty, tags
            FROM produkter
            WHERE product_id = %s
            """, (product_id,))
        product = cur.fetchone()
        cur.close()
        conn.close()

        # Stregkode (som før)
        if product and product.get('product_ean'):
            barcode_dir = os.path.join(app.static_folder, 'barcodes')
            fname = f"barcode {product['product_ean']}.png"  # eller .svg hvis du bruger svg
            fpath = os.path.join(barcode_dir, fname)
            if os.path.exists(fpath):
                image_url = url_for('static', filename=f'barcodes/{fname}')

    except Exception as e:
        error = f"Databasefejl: {e}"

    return render_template('product_detail.html', product=product, image_url=image_url, error=error)



@app.route('/products/<int:product_id>/edit', methods=['GET', 'POST'])
def products_edit(product_id):
    """
    Rediger produkt.
    """
    error = None
    product = None

    # hent eksisterende
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
                SELECT product_id, product_ean, product_name, product_desc, product_image, stock_qty, tags
                FROM produkter
                WHERE product_id = %s
                """, (product_id,))

        product = cur.fetchone()
        cur.close()
        conn.close()
    except Exception as e:
        error = f"Databasefejl: {e}"

    if not product:
        return render_template('product_form.html', mode='edit', error=error or "Produkt ikke fundet.", product=None)

    if request.method == 'POST':
        product_ean = (request.form.get('product_ean') or '').strip()
        product_name = (request.form.get('product_name') or '').strip()
        product_desc = (request.form.get('product_desc') or '').strip()

        tags_raw = request.form.get('tags')
        tags_list = parse_tags(tags_raw)  # None hvis feltet slet ikke var med

        # Hvis du vil beholde eksisterende tags når feltet er blankt:
        if tags_list is None:
            tags_param = product['tags']  # uændret
        else:
            tags_param = tags_list  # evt. []

        try:
            conn = get_db_connection()
            cur = conn.cursor()
            cur.execute("""
            UPDATE produkter
               SET product_ean  = %s,
                   product_name = %s,
                   product_desc = %s,
                   tags         = %s
             WHERE product_id  = %s
            """, (product_ean, product_name, product_desc, tags_param, product_id))
            conn.commit()
            cur.close(); conn.close()
            return render_template('redirect.html', target=url_for('products_detail', product_id=product_id))
        except Exception as e:
            error = f"Databasefejl: {e}"


    # hvis GET eller fejl, vis formular med nuværende værdier
    return render_template('product_form.html', mode='edit', error=error, product=product)


@app.route('/products/<int:product_id>/delete', methods=['POST'])
def products_delete(product_id):
    """
    Slet produkt (POST for at undgå utilsigtet sletning).
    """
    error = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("DELETE FROM produkter WHERE product_id = %s", (product_id,))
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        error = f"Databasefejl: {e}"
        # vis detail-side med fejl
        return render_template('product_detail.html', product={'product_id': product_id}, error=error)

    # tilbage til liste
    return render_template('redirect.html', target=url_for('products_list'))


# --------------- Run the App ---------------
if __name__ == '__main__':
    # Sørg for at mappen til stregkoder findes
    os.makedirs(os.path.join('static', 'barcodes'), exist_ok=True)
    app.run(debug=True)
