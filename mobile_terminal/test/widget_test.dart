// File: mobile_terminal/test/widget_test.dart

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:mobile_terminal/main.dart'; // Ensure this matches your package name

void main() {
  testWidgets('App smoke test', (WidgetTester tester) async {
    // Build our app and trigger a frame.
    await tester.pumpWidget(
      const TradeCoreApp(),
    ); // FIXED: Changed MyApp to TradeCoreApp

    // Verify that our app starts.
    expect(find.byType(MaterialApp), findsOneWidget);
  });
}
