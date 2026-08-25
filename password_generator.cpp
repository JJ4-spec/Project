#include <iostream>
#include <string>
#include <cstdlib>
using namespace std;

int main() {
	int length;
	string characters = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789";

	cout << "Enter the password length:";
	cin >> length;
        
        for (int i =0; i < length; i++) {
	    int  randomPosition =  rand() % 62;
	    cout << characters[randomPosition];
        }	      

        return 0;

}

