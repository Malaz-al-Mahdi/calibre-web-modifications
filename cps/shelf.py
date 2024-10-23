# -*- coding: utf-8 -*-

import sys
from datetime import datetime

from flask import Blueprint, flash, redirect, request, url_for, abort
from flask_babel import gettext as _
from .cw_login import current_user
from sqlalchemy.exc import InvalidRequestError, OperationalError
from sqlalchemy.sql.expression import func, true

from . import calibre_db, config, db, logger, ub
from .render_template import render_title_template
from .usermanagement import login_required_if_no_ano, user_login_required

log = logger.create()

shelf = Blueprint('shelf', __name__)

# Route zum Hinzufügen eines Buches zu einem Shelf
@shelf.route("/shelf/add/<int:shelf_id>/<int:book_id>", methods=["POST"])
@user_login_required
def add_to_shelf(shelf_id, book_id):
    xhr = request.headers.get('X-Requested-With') == 'XMLHttpRequest'
    shelf = ub.session.query(ub.Shelf).filter(ub.Shelf.id == shelf_id).first()
    
    if shelf is None:
        log.error("Invalid shelf specified: %s", shelf_id)
        if not xhr:
            flash(_("Invalid shelf specified"), category="error")
            return redirect(url_for('web.index'))
        return "Invalid shelf specified", 400

    if not check_shelf_edit_permissions(shelf):
        if not xhr:
            flash(_("Sorry you are not allowed to add a book to that shelf"), category="error")
            return redirect(url_for('web.index'))
        return "Sorry you are not allowed to add a book to that shelf", 403

    book_in_shelf = ub.session.query(ub.BookShelf).filter(
        ub.BookShelf.shelf == shelf_id,
        ub.BookShelf.book_id == book_id
    ).first()
    
    if book_in_shelf:
        log.error("Book %s is already part of %s", book_id, shelf)
        if not xhr:
            flash(_("Book is already part of the shelf: %(shelfname)s", shelfname=shelf.name), category="error")
            return redirect(url_for('web.index'))
        return "Book is already part of the shelf: %s" % shelf.name, 400

    max_order = ub.session.query(func.max(ub.BookShelf.order)).filter(
        ub.BookShelf.shelf == shelf_id
    ).scalar() or 0

    if not calibre_db.session.query(db.Books).filter(db.Books.id == book_id).one_or_none():
        log.error("Invalid Book Id: %s. Could not be added to shelf %s", book_id, shelf.name)
        if not xhr:
            flash(_("%(book_id)s is an invalid Book Id. Could not be added to Shelf", book_id=book_id),
                  category="error")
            return redirect(url_for('web.index'))
        return "%s is an invalid Book Id. Could not be added to Shelf" % book_id, 400

    shelf.books.append(ub.BookShelf(shelf=shelf.id, book_id=book_id, order=max_order + 1))
    shelf.last_modified = datetime.utcnow()
    
    try:
        ub.session.merge(shelf)
        ub.session.commit()
    except (OperationalError, InvalidRequestError) as e:
        ub.session.rollback()
        log.error("Settings Database error: %s", e)
        flash(_("Oops! Database Error: %(error)s.", error=str(e)), category="error")
        if "HTTP_REFERER" in request.environ:
            return redirect(request.environ["HTTP_REFERER"])
        else:
            return redirect(url_for('web.index'))
    
    if not xhr:
        log.debug("Book has been added to shelf: %s", shelf.name)
        flash(_("Book has been added to shelf: %(sname)s", sname=shelf.name), category="success")
        if "HTTP_REFERER" in request.environ:
            return redirect(request.environ["HTTP_REFERER"])
        else:
            return redirect(url_for('web.index'))
    return "", 204

# Route zum Erstellen eines neuen Shelfs
@shelf.route("/shelf/create", methods=["GET", "POST"])
@user_login_required
def create_shelf():
    shelf = ub.Shelf()  # Neues Shelf-Objekt erstellen
    return create_edit_shelf(shelf, page_title=_("Create a Shelf"), page="shelfcreate")

# Route zum Anzeigen eines spezifischen Shelfs
@shelf.route("/shelf/show/<int:shelf_id>", methods=["GET"])
@user_login_required
def show_shelf(shelf_id):
    return render_show_shelf(shelf_type=1, shelf_id=shelf_id, page_no=1, sort_param='abc')

# Hilfsfunktion zum Erstellen oder Bearbeiten eines Shelfs
def create_edit_shelf(shelf, page_title, page, shelf_id=False):
    if request.method == "POST":
        to_save = request.form.to_dict()
        is_public = 1 if to_save.get("is_public") == "on" else 0
        shelf.name = to_save.get("title", "")
        shelf.is_public = is_public

        if not shelf_id:
            shelf.user_id = int(current_user.id)
            ub.session.add(shelf)
        else:
            shelf.last_modified = datetime.utcnow()

        try:
            ub.session.commit()
            flash(_("Shelf %(title)s created", title=shelf.name), category="success")
            return redirect(url_for('shelf.show_shelf', shelf_id=shelf.id))
        except (OperationalError, InvalidRequestError) as ex:
            ub.session.rollback()
            flash(_("Database error: %(error)s", error=str(ex)), category="error")

    return render_title_template('shelf_edit.html', shelf=shelf, title=page_title, page=page)

# Funktion zur Anzeige eines Shelfs mit den entsprechenden Details
def render_show_shelf(shelf_type, shelf_id, page_no, sort_param):
    shelf = ub.session.query(ub.Shelf).filter(ub.Shelf.id == shelf_id).first()
    if not shelf or not check_shelf_view_permissions(shelf):
        flash(_("Error opening shelf. Shelf does not exist or is not accessible"), category="error")
        return redirect(url_for("web.index"))

    if shelf_type == 1:
        page, pagesize = "shelf.html", 0
    else:
        page, pagesize = "shelfdown.html", sys.maxsize

    result = calibre_db.session.query(
        db.Books.id,
        db.Books.title,
        db.Books.author_sort,
        db.Books.series,
        db.Books.series_index,
        db.Books.pubdate,
        db.Books.timestamp,
        db.Books.custom_column_4,
        db.Books.custom_column_2
    ).join(
        ub.BookShelf, ub.BookShelf.book_id == db.Books.id
    ).filter(
        ub.BookShelf.shelf == shelf_id
    ).order_by(
        ub.BookShelf.order.asc()
    ).all()

    return render_title_template(
        page,
        entries=result,
        title=_("Shelf: '%(name)s'", name=shelf.name),
        shelf=shelf,
        page="shelf"
    )

# Überprüft, ob der aktuelle Benutzer ein Shelf bearbeiten darf
def check_shelf_edit_permissions(cur_shelf):
    if not cur_shelf.is_public and cur_shelf.user_id != int(current_user.id):
        log.error("User %s not allowed to edit shelf: %s", current_user.id, cur_shelf.name)
        return False
    if cur_shelf.is_public and not current_user.role_edit_shelfs():
        log.info("User %s not allowed to edit public shelves", current_user.id)
        return False
    return True

# Überprüft, ob der aktuelle Benutzer ein Shelf anzeigen darf
def check_shelf_view_permissions(cur_shelf):
    if cur_shelf.is_public:
        return True
    if current_user.is_anonymous or cur_shelf.user_id != current_user.id:
        log.error("User is unauthorized to view non-public shelf: %s", cur_shelf.name)
        return False
    return True
