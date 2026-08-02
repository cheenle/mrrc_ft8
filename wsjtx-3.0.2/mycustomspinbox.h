#ifndef MYCUSTOMSPINBOX_H
#define MYCUSTOMSPINBOX_H

#include <QSpinBox>
#include <QString>
#include <QLocale>

class MyCustomSpinBox : public QSpinBox
{
  Q_OBJECT
 public:
  explicit MyCustomSpinBox(QWidget *parent = nullptr);

 protected:
  QString textFromValue(int val) const override;
};

#endif // MYCUSTOMSPINBOX_H
