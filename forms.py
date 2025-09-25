from flask_wtf import FlaskForm
from wtforms import StringField, PasswordField, SubmitField
from wtforms.validators import DataRequired

class LoginForm(FlaskForm):
    username = StringField('Utilisateur', validators=[DataRequired(message="Le nom d'utilisateur est requis.")])
    password = PasswordField('Mot de passe', validators=[DataRequired(message="Le mot de passe est requis.")])
    submit = SubmitField('Connexion')