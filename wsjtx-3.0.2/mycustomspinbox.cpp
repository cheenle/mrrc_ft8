#include "mycustomspinbox.h"

MyCustomSpinBox::MyCustomSpinBox(QWidget *parent)
  : QSpinBox(parent)
{
}

QString MyCustomSpinBox::textFromValue(int val) const
{
  // Determine the desired number of digits (e.g., 2 for "01", "09", "10")
  // You can make this dynamic based on the maximum value or a fixed number.
  int desiredDigits = 3; // Example: always show at least 3 digits

  QString formattedString = QString("%1").arg(val, desiredDigits, 10, QChar('0'));
  return formattedString;
}
